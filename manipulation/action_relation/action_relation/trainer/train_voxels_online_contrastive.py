import numpy as np
import argparse
import pickle
import h5py
import sys
import os
import pprint
import torch
import torch.nn as nn
from torchvision.transforms import functional as F
import torch.optim as optim
import time
import json
import copy
from tqdm import tqdm

sys.path.append(os.getcwd())

from robot_learning.logger.tensorboardx_logger import TensorboardXLogger
from utils.torch_utils import get_weight_norm_for_network, to_numpy
from utils.colors import bcolors
from utils.data_utils import str2bool
from utils.data_utils import recursively_save_dict_contents_to_group
from utils.data_utils import convert_list_of_array_to_dict_of_array_for_hdf5
from utils.image_utils import get_image_tensor_mask_for_bb


from action_relation.dataloader.vrep_dataloader import SimpleVoxelDataloader
from action_relation.dataloader.orient_change_based_vrep_dataloader import OrientChangeBasedVrepDataloader 
from action_relation.model.voxel_model import VoxelModel
from action_relation.model.unscaled_voxel_model import BoundingBoxOnlyModel
from action_relation.model.unscaled_voxel_model import UnscaledVoxelModel, SmallEmbUnscaledVoxelModel
from action_relation.model.unscaled_voxel_model import UnscaledResNetVoxelModel
from action_relation.model.unscaled_voxel_model import get_unscaled_resnet10
from action_relation.model.unscaled_voxel_model import get_unscaled_resnet18
from action_relation.model.contact_model import ContactDistributionPredictionModel
from action_relation.model.contact_model import ContactForceTorquePredictionModel
from action_relation.model.model_utils import ScaledMSELoss
from action_relation.model.losses import TripletLoss, get_contrastive_loss
from action_relation.utils.data_utils import get_euclid_dist_matrix_for_data

from vae.config.base_config import BaseVAEConfig
from vae.trainer.base_train import create_log_dirs, add_common_args_to_parser
from vae.trainer.base_train import BaseVAETrainer


def fn_voxel_parse(voxel_obj):
    status, a = voxel_obj.parse()
    return a

# from multiprocessing import Pool


def array_fmt(arr, p):
    return np.array2string(arr, precision=p, separator=',', suppress_small=True,
                           max_line_width=120)


class VoxelRelationTrainer(BaseVAETrainer):
    def __init__(self, config):
        super(VoxelRelationTrainer, self).__init__(config)
        self.hidden_dim = 64
        args = config.args

        if args.dataloader == 'simple':
            self.dataloader = SimpleVoxelDataloader(
                config,
                voxel_datatype_to_use=args.voxel_datatype)
        elif args.dataloader == 'orient_change_sampler':
            self.dataloader = OrientChangeBasedVrepDataloader(
                config,
                voxel_datatype_to_use=args.voxel_datatype)

        args = config.args
        model = args.model
        # model = 'simple_model'
        if args.voxel_datatype == 0:
            self.model = create_model_with_checkpoint(model, args.checkpoint_path, args)

        elif args.voxel_datatype == 1:
            self.model = VoxelModel(args.z_dim, 6, args,
                                    n_classes=2*args.classif_num_classes)
        else:
            raise ValueError("Invalid voxel datatype: {}".format(
                args.voxel_datatype))
        
        if args.use_contact_preds:
            self.contact_model = ContactDistributionPredictionModel(
                args.z_dim, 
                3 + 9,  # mean + cov matrix
                args,
            )
        else:
            self.contact_model = None
        
        if args.use_ft_sensor:
            self.ft_sensor_model = ContactForceTorquePredictionModel(
                args.z_dim + 6,  # 3 for action
                6,
                args
            )
        else:
            self.ft_sensor_model = None

        # self.loss = nn.BCELoss()
        if args.loss_type == 'regr':
            if args.scaled_mse_loss:
                raise ValueError("Do not use")
                self.loss = ScaledMSELoss(0.0001)
                self.pose_pred_loss = ScaledMSELoss(0.0001)
            elif args.use_l1_loss:
                self.loss = nn.MSELoss()
                self.pose_pred_loss = nn.L1Loss()
            else:
                raise ValueError("Do not use")
                self.loss = nn.MSELoss()
                self.pose_pred_loss = nn.MSELoss()
        elif args.loss_type == 'classif':
            self.pose_pred_loss = nn.CrossEntropyLoss()

        self.inv_model_loss = nn.MSELoss()

        self.contrastive_margin = args.contrastive_margin
        self.triplet_loss = TripletLoss(self.contrastive_margin)
        self.ft_sensor_loss = nn.MSELoss()
        self.contact_dist_loss = nn.MSELoss()

        self.opt = optim.Adam(self.model.parameters(), lr=config.args.lr)
        lr_lambda = lambda epoch: 0.98**epoch
        self.lr_scheduler = optim.lr_scheduler.LambdaLR(self.opt, lr_lambda)

        if args.use_contact_preds:
            self.contact_opt = optim.Adam(self.contact_model.parameters(), 
                                          lr=config.args.lr)
            self.contact_lr_scheduler = optim.lr_scheduler.LambdaLR(
                self.contact_opt, lr_lambda)
        if args.use_ft_sensor:
            self.ft_sensor_opt = optim.Adam(self.ft_sensor_model.parameters(), 
                                            lr=config.args.lr)
            self.ft_sensor_lr_scheduler = optim.lr_scheduler.LambdaLR(
                self.ft_sensor_opt, lr_lambda)

    def get_all_model_dict(self):
        model_dict = {'model': self.model}
        if args.use_contact_preds:
            model_dict['contact_model'] = self.contact_model
        if args.use_ft_sensor:
            model_dict['ft_sensor_model'] = self.ft_sensor_model
        return model_dict

    def get_model_data_to_save(self):
        device = self.config.get_device()
        cpu_device = torch.device("cpu")
        model_dict = self.get_all_model_dict()
        model_state_dict = {}
        for model_key, m in model_dict.items():
            model = m.to(cpu_device)
            model_state_dict[model_key] = model.state_dict()
            m.to(device)

        return model_state_dict

    def save_checkpoint(self, epoch, data=None):
        cp_filepath = self.model_checkpoint_filename(epoch)
        model_data = self.get_model_data_to_save()
        torch.save(model_data, cp_filepath)
        print(bcolors.c_red("Save checkpoint: {}".format(cp_filepath)))

    def load_checkpoint(self, checkpoint_path):
        checkpoint_models = torch.load(checkpoint_path, 
                                       map_location=torch.device('cpu'))
        self.model.load_state_dict(checkpoint_models['model'])
        if checkpoint_models.get('contact_model') is not None:
            self.contact_model.load_state_dict(checkpoint_models['contact_model'])
        if checkpoint_models.get('ft_sensor_model') is not None:
            self.ft_sensor_model.load_state_dict(checkpoint_models['ft_sensor_model'])
    
    def _log_model_to_tensorboard(self, train_step_count, model, model_key, model_opt):
        k = model_key
        l2_norm, grad_l2_norm = get_weight_norm_for_network(model)
        self.logger.summary_writer.add_scalar(
                '{}/weight'.format(k), l2_norm, train_step_count)
        self.logger.summary_writer.add_scalar(
                '{}/grad'.format(k), grad_l2_norm, train_step_count)
        self.logger.summary_writer.add_scalar(
            '{}/lr'.format(k), model_opt.param_groups[0]['lr'], train_step_count)

    def log_model_to_tensorboard(self, train_step_count):
        '''Log weights and gradients of network to Tensorboard.'''
        model_l2_norm, model_grad_l2_norm = \
            get_weight_norm_for_network(self.model)
        # if args.model_type == 'vae':
        #     self.logger.summary_writer.add_histogram(
        #             'histogram/mu_linear_layer_weights',
        #             self.model.mu_linear.weight,
        #             train_step_count)
        #     self.logger.summary_writer.add_histogram(
        #             'histogram/logvar_linear_layer_weights',
        #             self.model.logvar_linear.weight,
        #             train_step_count)
        args = self.config.args
        self._log_model_to_tensorboard(train_step_count, self.model, 'model', self.opt)
        if args.use_contact_preds:
            self._log_model_to_tensorboard(train_step_count, self.contact_model, 
                                           'contact_model', self.contact_opt)
        if args.use_ft_sensor:
            self._log_model_to_tensorboard(train_step_count, self.ft_sensor_model, 
                                           'ft_sensor_model', self.ft_sensor_opt)

    def process_raw_voxels(self, voxel_data):
        args = self.config.args
        if len(voxel_data.size()) == 3:
            voxels = voxel_data.squeeze(0)
        elif len(voxel_data.size()) == 4:
            voxels = voxel_data
        else:
            raise ValueError("Invalid voxels size: {}".format(
                voxel_data.size()))

        if args.add_xy_channels == 0:
            pass
        elif args.add_xy_channels == 1:
            if args.voxel_datatype == 0:
                pos_grid_tensor = self.dataloader.pos_grid
                voxels = torch.cat([voxels, pos_grid_tensor], dim=0)
        else:
            raise ValueError("Invalid add_xy_channels: {}".format(
                args.add_xy_channels))
        return voxels

    def process_raw_batch_data(self, batch_data):
        '''Process raw batch data and collect relevant objects in a dict.'''
        proc_batch_dict = {
            # Save input image
            'batch_voxel_list': [],
            # Save object pose before action.
            'batch_obj_before_pose_list': [],
            # Save object pose after action.
            'batch_obj_after_pose_list': [],
            # Save object change pose
            'batch_obj_delta_pose_list': [],
            # Save object change pose for contrastive estimation
            'batch_obj_delta_pose_contrastive_list': [],
            # Save object delta pose class.
            'batch_obj_delta_pose_class_list': [],
            # Save action info.
            'batch_action_list': [],
            # Save action index info.
            'batch_action_index_list': [],
            # Save object bounding boxes.
            'batch_bb_list': [],
            # FT sensor readings
            'batch_ft_sensor_list': [],
            # Contact data
            'batch_contact_mu_list': [],
            'batch_contact_cov_list': [],
            'batch_contact_mask_list': [],
        }
        args = self.config.args
        x_dict = proc_batch_dict

        for b, data in enumerate(batch_data):
            x_dict['batch_obj_before_pose_list'].append(data['before_pose'])
            x_dict['batch_obj_after_pose_list'].append(data['after_pose'])
            x_dict['batch_obj_delta_pose_list'].append(data['delta_pose'])
            x_dict['batch_obj_delta_pose_contrastive_list'].append(
                data['delta_pose_contrastive'])
            x_dict['batch_action_list'].append(data['action'])
            x_dict['batch_action_index_list'].append(data['action_idx'])
            x_dict['batch_obj_delta_pose_class_list'].append(data['delta_classes'])
            x_dict['batch_bb_list'].append(data['bb_list'])

            if data.get('ft_sensor'):
                x_dict['batch_ft_sensor_list'].append(data['ft_sensor'])

            if data.get('contact_data') is not None:
                x_dict['batch_contact_mu_list'].append(data['contact_dict']['mu'])
                x_dict['batch_contact_cov_list'].append(data['contact_dict']['cov'])
                x_dict['batch_contact_mask_list'].append(data['contact_dict']['mask'])
                assert x_dict['batch_contact_mask_list'][-1] == 1
            else:
                x_dict['batch_contact_mask_list'].append(0)

            # import ipdb; ipdb.set_trace()

            voxels = self.process_raw_voxels(data['voxels'])
            x_dict['batch_voxel_list'].append(voxels)

        return x_dict

    def collate_batch_data_to_tensors(self, proc_batch_dict):
        '''Collate processed batch into tensors.'''
        # Now collate the batch data together
        x_tensor_dict = {}
        x_dict = proc_batch_dict
        device = self.config.get_device()
        args = self.config.args

        x_tensor_dict['batch_voxel'] = torch.stack(x_dict['batch_voxel_list']).to(device)
        x_tensor_dict['batch_obj_before_pose_list'] = torch.Tensor(
            x_dict['batch_obj_before_pose_list']).to(device)
        x_tensor_dict['batch_obj_delta_pose_list'] = torch.Tensor(
            x_dict['batch_obj_delta_pose_list']).to(device)
        x_tensor_dict['batch_obj_delta_pose_contrastive_list'] = torch.Tensor(
            x_dict['batch_obj_delta_pose_contrastive_list']).to(device)
        x_tensor_dict['batch_action_list'] = torch.Tensor(
            x_dict['batch_action_list']).to(device)
        x_tensor_dict['batch_action_index_list'] = torch.LongTensor(
            x_dict['batch_action_index_list']).to(device)
        x_tensor_dict['batch_bb_list'] = torch.Tensor(
            x_dict['batch_bb_list']).to(device)
        if x_dict.get('batch_obj_delta_pose_class_list') is not None:
            x_tensor_dict['batch_obj_delta_pose_class_list'] = torch.LongTensor(
                x_dict['batch_obj_delta_pose_class_list']).to(device)

        if x_dict.get('batch_ft_sensor_list') is not None and \
            len(x_dict['batch_ft_sensor_list']) > 0:
            x_tensor_dict['batch_ft_sensor_list'] = torch.Tensor(
                x_dict['batch_ft_sensor_list']).to(device)

        if x_dict.get('batch_contact_mu_list') is not None and \
            len(x_dict['batch_contact_mu_list']) > 0:
            x_tensor_dict['batch_contact_mu_list'] = torch.Tensor(
                x_dict['batch_contact_mu_list']).to(device)
            x_tensor_dict['batch_contact_cov_list'] = torch.Tensor(
                x_dict['batch_contact_cov_list']).to(device)
            x_tensor_dict['batch_contact_mask_list'] = torch.LongTensor(
                x_dict['batch_contact_mask_list']).to(device)

        return x_tensor_dict
    
    def get_embedding_for_data(self, before_voxels):
        '''Get embedding for voxel data.'''
        device = self.config.get_device()
        args = self.config.args
        # Add the batch dimension
        if len(before_voxels.size()) == 4:
            voxel_data = before_voxels.unsqueeze(0).to(device)
        else:
            voxel_data = before_voxels.to(device)
        
        with torch.no_grad():
            img_emb = self.model.forward_image(voxel_data, relu_on_emb=True)
        return img_emb

    def predict_model_on_data(self, before_voxels, action):
        '''Predict model output on single data item. 
        
        Useful for online training.
        '''

        device = self.config.get_device()
        args = self.config.args

        # Add the batch dimension
        assert len(before_voxels.size()) == 4
        voxel_data = before_voxels.unsqueeze(0).to(device)
        if len(action.size()) == 1:
            action = action.unsqueeze(0).to(device)
        else:
            action = action.to(device)

        with torch.no_grad():
            img_emb = self.model.forward_image(voxel_data)

            if args.use_bb_in_input:
                raise ValueError("Not implemented yet")
            else:
                img_emb_with_action = torch.cat([img_emb, action] , dim=1)

            img_action_emb = self.model.forward_image_with_action(img_emb_with_action)
            pred_delta_pose = self.model.forward_predict_delta_pose(img_action_emb)

        pred_delta_pose_numpy = to_numpy(pred_delta_pose)
        return pred_delta_pose, pred_delta_pose_numpy


    def run_model_on_contrastive_batch(self, x_tensor_dict, batch_size,
                                       train=True, save_preds=True):
        batch_result_dict = {}
        device = self.config.get_device()
        args = self.config.args

        anchor_emb = self.model.forward_image(x_tensor_dict[0]['batch_voxel'])
        sim_img_emb = self.model.forward_image(x_tensor_dict[1]['batch_voxel'])
        diff_img_emb = self.model.forward_image(x_tensor_dict[2]['batch_voxel'])

        triplet_loss = args.weight_contrastive_loss * \
            self.triplet_loss(anchor_emb, sim_img_emb, diff_img_emb)

        total_loss = triplet_loss

        if train:
            self.opt.zero_grad()
            total_loss.backward()
            self.opt.step()

        batch_result_dict = {
            'triplet_loss': triplet_loss.item(),
        }

        return batch_result_dict
    
    def run_model_on_batch(self,
                           x_tensor_dict,
                           batch_size,
                           train=False,
                           save_preds=False):
        batch_result_dict = {}
        device = self.config.get_device()
        args = self.config.args

        voxel_data = x_tensor_dict['batch_voxel']
        if args.model == 'bb_only':
            img_emb = None
        else:
            img_emb = self.model.forward_image(voxel_data)
        # TODO: Add hinge loss
        # img_emb = img_emb.squeeze()

        if img_emb is None:
            img_emb_with_action = torch.cat([
                x_tensor_dict['batch_bb_list'],
                x_tensor_dict['batch_obj_before_pose_list'],
                x_tensor_dict['batch_action_list']], dim=1)
        elif args.use_bb_in_input:
            img_emb_with_action = torch.cat([
                img_emb,
                x_tensor_dict['batch_bb_list'],
                x_tensor_dict['batch_obj_before_pose_list'],
                x_tensor_dict['batch_action_list']], dim=1)
        else:
            img_emb_with_action = torch.cat([
                img_emb,
                x_tensor_dict['batch_action_list']], dim=1)

        if args.use_contrastive_loss:
            triplet_loss = args.weight_contrastive_loss * get_contrastive_loss(
                img_emb,
                x_tensor_dict['batch_obj_delta_pose_contrastive_list'][:, :3],
                x_tensor_dict['batch_action_index_list'],
                args.contrastive_margin,
                args.contrastive_gt_pose_margin,
            )
        else:
            triplet_loss = 0.
        
        if args.use_orient_contrastive_loss:
            orient_triplet_loss = args.weight_orient_contrastive_loss * get_contrastive_loss(
                img_emb,
                x_tensor_dict['batch_obj_delta_pose_contrastive_list'][:, 3:6],
                x_tensor_dict['batch_action_index_list'],
                args.orient_contrastive_margin,
                args.orient_contrastive_gt_pose_margin,
            )
        else:
            orient_triplet_loss = 0.
        
        if args.use_contact_preds:
            contact_preds = self.contact_model(img_emb)
            contact_gt_mu = x_tensor_dict['batch_contact_mu_list']
            contact_gt_cov = x_tensor_dict['batch_contact_cov_list']
            contact_gt_mask = x_tensor_dict['batch_contact_mask_list']
            # Select the right indexes from contact_preds
            contact_mu_loss = args.weight_contact_mu * self.contact_dist_loss(
                contact_preds[:, :3], contact_gt_mu)
            contact_cov_loss = args.weight_contact_cov * self.contact_dist_loss(
                contact_preds[:, 3:], contact_gt_cov)
            
        if args.use_ft_sensor:
            ft_preds = self.ft_sensor_model(img_emb_with_action)
            ft_gt = x_tensor_dict['batch_ft_sensor_list']
            ft_force_loss = args.weight_ft_force * self.ft_sensor_loss(
                ft_preds[:, :3], ft_gt[:, :3]
            )
            ft_torque_loss = args.weight_ft_torque * self.ft_sensor_loss(
                ft_preds[:, 3:6], ft_gt[:, 3:6]
            )

        # For SmallEmbUnscaledVoxelModel forward_image_with_action does nothing 
        # but returns the input.
        img_action_emb = self.model.forward_image_with_action(img_emb_with_action)
        pred_delta_pose = self.model.forward_predict_delta_pose(img_action_emb)
        # pose_pred_loss = args.weight_bb * self.pose_pred_loss(
        #     pred_delta_pose,  x_tensor_dict['batch_obj_delta_pose_list'])

        if args.loss_type == 'regr':
            position_pred_loss = args.weight_pos * self.loss(
                pred_delta_pose[:, :3],
                x_tensor_dict['batch_obj_delta_pose_list'][:, :3]
            )
            angle_pred_loss = args.weight_angle * self.pose_pred_loss(
                pred_delta_pose[:, 3:],
                x_tensor_dict['batch_obj_delta_pose_list'][:, 3:6]
            )
        elif args.loss_type == 'classif':
            n_classes = args.classif_num_classes

            true_label_x = x_tensor_dict['batch_obj_delta_pose_class_list'][:, 0]
            true_label_y = x_tensor_dict['batch_obj_delta_pose_class_list'][:, 1]
            position_pred_loss_x = args.weight_pos * self.pose_pred_loss(
                pred_delta_pose[:, :n_classes], true_label_x)
            position_pred_loss_y = args.weight_pos * self.pose_pred_loss(
                pred_delta_pose[:, n_classes:], true_label_y)
            position_pred_loss = position_pred_loss_x + position_pred_loss_y

            angle_pred_loss = 0

        else:
            raise ValueError("Invalid loss type: {}".format(args.loss_type))

        # img_action_with_delta_pose = torch.cat(
        #    [img_action_emb, x_tensor_dict['batch_other_bb_pred_list']], dim=1)
        # pred_img_emb = self.model.forward_predict_original_img_emb(
        #    img_action_with_delta_pose)
        #inv_model_loss = args.weight_inv_model * self.inv_model_loss(
        #   pred_img_emb, img_emb)
        inv_model_loss = 0.

        # total_loss = pose_pred_loss + inv_model_loss
        total_loss = position_pred_loss \
            + angle_pred_loss \
            + triplet_loss \
            + orient_triplet_loss 

        if args.use_contact_preds:
            total_loss += (contact_mu_loss + contact_cov_loss)
        if args.use_ft_sensor:
            total_loss += (ft_force_loss + ft_torque_loss)

        if train:
            self.opt.zero_grad()
            if args.use_contact_preds:
                self.contact_opt.zero_grad()
            if args.use_ft_sensor:
                self.ft_sensor_opt.zero_grad()
            total_loss.backward()
            self.opt.step()
            if args.use_contact_preds:
                self.contact_opt.step()
            if args.use_ft_sensor:
                self.ft_sensor_opt.step()

        batch_result_dict['img_emb'] = img_emb
        batch_result_dict['img_action_emb'] = img_action_emb
        batch_result_dict['pred_delta_pose'] = pred_delta_pose
        batch_result_dict['pose_pred_loss'] = position_pred_loss + angle_pred_loss
        batch_result_dict['position_pred_loss'] = position_pred_loss
        if args.loss_type == 'classif':
            batch_result_dict['position_pred_loss_x'] = position_pred_loss_x
            batch_result_dict['position_pred_loss_y'] = position_pred_loss_y

            # Save conf matrix.
            _, pred_x = torch.max(pred_delta_pose[:, :n_classes], dim=1)
            _, pred_y = torch.max(pred_delta_pose[:, n_classes:], dim=1)
            conf_x = np.zeros((n_classes, n_classes), dtype=np.int32)
            conf_y = np.zeros((n_classes, n_classes), dtype=np.int32)
            for b in range(true_label_x.size(0)):
                conf_x[true_label_x[b].item(), pred_x[b].item()] += 1
                conf_y[true_label_y[b].item(), pred_y[b].item()] += 1
            batch_result_dict['conf_x'] = conf_x
            batch_result_dict['conf_y'] = conf_y


        batch_result_dict['angle_pred_loss'] = angle_pred_loss
        batch_result_dict['inv_model_loss'] = inv_model_loss
        batch_result_dict['triplet_loss'] = triplet_loss
        batch_result_dict['orient_triplet_loss'] = orient_triplet_loss
        batch_result_dict['total_loss'] = total_loss
        if args.use_contact_preds:
            batch_result_dict['contact_mu_loss'] = contact_mu_loss
            batch_result_dict['contact_cov_loss'] = contact_cov_loss
        if args.use_ft_sensor:
            batch_result_dict['ft_force_loss'] = ft_force_loss
            batch_result_dict['ft_torque_loss'] = ft_torque_loss
        
        # Sum up the orientation error for cases where the orientation 
        # changes greater than a threshold.
        orient_th = 1e-3
        batch_result_dict['orient_gt_th'] = {'error': [], 'count': [], 
                                             'lt_error': [], 'lt_count': []}
        for axes in range(3, 6):
            orient_gt = x_tensor_dict['batch_obj_delta_pose_list'][:, axes]
            orient_idx = torch.where(torch.abs(orient_gt) > orient_th)[0]
            orient_pred = pred_delta_pose[:, axes]
            err = torch.sum(torch.abs(
                orient_pred[orient_idx] - orient_gt[orient_idx])).detach().item()
            total_err = torch.sum(torch.abs(orient_pred - orient_gt)).detach().item()
            batch_result_dict['orient_gt_th']['error'].append(err)
            batch_result_dict['orient_gt_th']['lt_error'].append(total_err-err)
            batch_result_dict['orient_gt_th']['count'].append(orient_idx.size(0))
            batch_result_dict['orient_gt_th']['lt_count'].append(
                orient_gt.size(0)-orient_idx.size(0))
        # Convert stuff to array for easier manipulation later.
        for k in ['error', 'count', 'lt_error', 'lt_count']:
            batch_result_dict['orient_gt_th'][k] = np.array(
                batch_result_dict['orient_gt_th'][k])
        
        # Sum up the torque error for cases where the torque is greater than 
        # a threshold.
        if args.use_ft_sensor:
            batch_result_dict['torque_gt_th'] = {'error': [], 'count': []}
            torque_th = 1e-3
            for axes in range(3, 6):
                torque_gt = x_tensor_dict['batch_ft_sensor_list'][:, axes]
                torque_idx = torch.where(torch.abs(torque_gt) > torque_th)[0]
                torque_pred = ft_preds[:, axes]
                err = torch.sum(torch.abs(torque_pred[torque_idx] - torque_gt[torque_idx]))
                batch_result_dict['torque_gt_th']['error'].append(err.detach().item())
                batch_result_dict['torque_gt_th']['count'].append(torque_idx.size(0))
            for k in ['error', 'count']:
                batch_result_dict['torque_gt_th'][k] = np.array(
                    batch_result_dict['torque_gt_th'][k])
            


        if not train and save_preds:
            batch_result_dict['pos_gt'] = \
                to_numpy(x_tensor_dict['batch_obj_delta_pose_list'][:, :3])
            batch_result_dict['pos_class_gt'] = \
                to_numpy(x_tensor_dict['batch_obj_delta_pose_class_list'])
            batch_result_dict['orient'] = {}
            batch_result_dict['orient']['gt'] = \
                to_numpy(x_tensor_dict['batch_obj_delta_pose_list'][:, 3:])
            if args.loss_type == 'regr':
                batch_result_dict['pos_pred'] = to_numpy(pred_delta_pose[:, :3])
                batch_result_dict['orient']['pred'] = to_numpy(pred_delta_pose[:, 3:])

                # Get l1 and l2 error without averaging over the axes.
                pred_err = batch_result_dict['pos_gt'] - batch_result_dict['pos_pred']
                batch_result_dict['pred_l1_error'] = np.sum(np.abs(pred_err), axis=0)
                batch_result_dict['pred_l2_error'] = np.sum(pred_err**2, axis=0)
                orient_pred_err = batch_result_dict['orient']['gt'] - \
                    batch_result_dict['orient']['pred']
                batch_result_dict['orient']['pred_l1_error'] = \
                    np.sum(np.abs(orient_pred_err), axis=0)
                batch_result_dict['orient']['pred_l2_error'] = \
                    np.sum(orient_pred_err**2, axis=0)
            else:
                batch_result_dict['pos_pred'] = to_numpy(pred_delta_pose)
            
            if args.use_contact_preds:
                batch_result_dict['contact_gt_mu'] = to_numpy(contact_gt_mu)
                batch_result_dict['contact_gt_cov'] = to_numpy(contact_gt_cov)
                batch_result_dict['contact_gt_mask'] = to_numpy(contact_gt_mask)
                batch_result_dict['contact_preds'] = to_numpy(contact_preds)

            if args.use_ft_sensor:
                batch_result_dict['ft_gt'] = to_numpy(ft_gt)
                batch_result_dict['ft_preds'] = to_numpy(ft_preds)

        return batch_result_dict

    def print_train_update_to_console(self, current_epoch, num_epochs,
                                      curr_batch, num_batches, train_step_count,
                                      batch_result_dict, train=True):
        args = self.config.args
        e, batch_idx = current_epoch, curr_batch
        total_loss = batch_result_dict['total_loss']

        color_fn = bcolors.okblue if train else bcolors.c_yellow

        if train_step_count % args.print_freq_iters == 0:
            if args.loss_type == 'regr' and args.use_contrastive_loss and \
               args.use_orient_contrastive_loss:
                print(color_fn(
                    "[{}/{}], \t Batch: [{}/{}], \t    total_loss: {:.4f},"
                    "\tbb: {:.4f}, \t   pos: {:.4f}, orient: {:.4f}, "
                    "\t  trip pos: {:.4f},  orient: {:.4f}".format(
                        e, num_epochs, batch_idx, num_batches, total_loss,
                        batch_result_dict['pose_pred_loss'],
                        batch_result_dict['position_pred_loss'],
                        batch_result_dict['angle_pred_loss'],
                        batch_result_dict['triplet_loss'],
                        batch_result_dict['orient_triplet_loss'],)))

                orient_gt_th_error = batch_result_dict['orient_gt_th']['error']
                orient_lt_th_error = batch_result_dict['orient_gt_th']['lt_error']
                orient_gt_th_count = batch_result_dict['orient_gt_th']['count']
                orient_lt_th_count = batch_result_dict['orient_gt_th']['lt_count']
                orient_gt_th_count[orient_gt_th_count == 0] = 1
                orient_err_ratio = orient_gt_th_error/orient_gt_th_count
                orient_lt_err_ratio = orient_lt_th_error/orient_lt_th_count
                print(bcolors.c_purple(
                    "   \t  orient error:  great => mean: {} "
                    "\t count: {}, \t  less =>  mean: {}, count: {}".format(
                        array_fmt(orient_err_ratio, 6),
                        array_fmt(orient_gt_th_count, 0), 
                        array_fmt(orient_lt_err_ratio, 6),
                        array_fmt(orient_lt_th_count, 0), 
                        )))

                if args.use_ft_sensor: 
                    torque_gt_th_error = batch_result_dict['torque_gt_th']['error']
                    torque_gt_th_count = batch_result_dict['torque_gt_th']['count']
                    torque_gt_th_count[torque_gt_th_count == 0] = 1
                    torque_err_ratio = torque_gt_th_error/torque_gt_th_count
                    print(bcolors.c_purple(
                        "   \t  \t  torque_error_gt_th  \t  sum: {}, "
                        "\t count: {}, \t   mean: {}".format(
                            array_fmt(torque_gt_th_error, 4), 
                            array_fmt(torque_gt_th_count, 0), 
                            array_fmt(torque_err_ratio, 6),
                            )))

            elif args.loss_type == 'regr' and args.use_contrastive_loss:
                print(color_fn(
                    "[{}/{}], \t Batch: [{}/{}], \t    total_loss: {:.4f}, "
                    "\t  bb: {:.4f}, \t   position: {:.4f}, angle: {:.2f}, "
                    "triplet: {:.4f}".format(
                        e, num_epochs, batch_idx, num_batches, total_loss,
                        batch_result_dict['pose_pred_loss'],
                        batch_result_dict['position_pred_loss'],
                        batch_result_dict['angle_pred_loss'],
                        batch_result_dict['triplet_loss'])))
            elif args.loss_type == 'regr':
                print(color_fn(
                    "[{}/{}], \t Batch: [{}/{}], \t    total_loss: {:.4f}, "
                    "\t  bb: {:.4f}, \t   position: {:.3f}, angle: {:.3f}".format(
                        e, num_epochs, batch_idx, num_batches, total_loss,
                        batch_result_dict['pose_pred_loss'],
                        batch_result_dict['position_pred_loss'],
                        batch_result_dict['angle_pred_loss'])))
            elif args.loss_type == 'classif':
                print(color_fn(
                    "[{}/{}], \t Batch: [{}/{}], \t    total_loss: {:.4f}, "
                    "\t  bb: {:.4f}, \t   pos_x: {:.4f}, pos_y: {:.4f}".format(
                        e, num_epochs, batch_idx, num_batches, total_loss,
                        batch_result_dict['pose_pred_loss'],
                        batch_result_dict['position_pred_loss_x'],
                        batch_result_dict['position_pred_loss_y'])))
            
            if args.use_contact_preds or args.use_ft_sensor:
                print_str = ''
                if args.use_contact_preds:
                    print_str += "\t  contact_mu: {:.4f}, contact_cov: {:.4f}".format(
                        batch_result_dict['contact_mu_loss'],
                        batch_result_dict['contact_cov_loss'])
                    print_str += '   \t   ' 
                if args.use_ft_sensor:
                    print_str += "\t  ft_force: {:.4f},  ft_torque: {:.4f}".format(
                        batch_result_dict['ft_force_loss'],
                        batch_result_dict['ft_torque_loss'])
                print(color_fn(print_str))


    def plot_train_update_to_tensorboard(self, x_dict, x_tensor_dict,
        batch_result_dict, step_count, log_prefix='',
        plot_images=True, plot_loss=True):
        args = self.config.args

        if plot_images:
            # self.logger.summary_writer.add_images(
            #         log_prefix + '/image/input_image',
            #         x_tensor_dict['batch_img'].clone().cpu()[:,:3,:,:],
            #         step_count)
            pass
        
        def _loss_logger_fn(k):
            self.logger.summary_writer.add_scalar(
                log_prefix+'loss/{}'.format(k), batch_result_dict[k], step_count)

        if plot_loss:
            _loss_logger_fn('pose_pred_loss')
            _loss_logger_fn('position_pred_loss')

            if args.loss_type == 'classif':
                for k in ['position_pred_loss_x', 'position_pred_loss_y']:
                    self.logger.summary_writer.add_scalar(
                        log_prefix+'loss/{}'.format(k),
                        batch_result_dict[k],
                        step_count
                    )

            _loss_logger_fn('angle_pred_loss')
            # _loss_logger_fn('inv_model_loss')
            _loss_logger_fn('total_loss')

            if args.use_contrastive_loss:
                _loss_logger_fn('triplet_loss')
            if args.use_orient_contrastive_loss:
                _loss_logger_fn('orient_triplet_loss')
            if args.use_contact_preds:
                _loss_logger_fn('contact_mu_loss')
                _loss_logger_fn('contact_cov_loss')
            if args.use_ft_sensor:
                _loss_logger_fn('ft_force_loss')
                _loss_logger_fn('ft_torque_loss')

    def get_online_contrastive_samples(self, batch_online_contrastive_data,
                                       contrastive_data_log_idx,
                                       sample_hard_examples_only=True,
                                       sample_all_examples=False,
                                       squared=True):
        assert ((sample_hard_examples_only and not sample_all_examples) or
                (not sample_hard_examples_only and sample_all_examples))

        num_data = len(batch_online_contrastive_data['info'])
        all_emb = np.vstack(batch_online_contrastive_data['img_emb'])
        emb_dot_prod = np.dot(all_emb, all_emb.T)

        sq_norm = np.diagonal(emb_dot_prod)

        emb_dist = sq_norm[None, :] - 2*emb_dot_prod + sq_norm[:, None]
        emb_dist = np.maximum(emb_dist, 0.0)

        if not squared:
            emb_dist = np.sqrt(emb_dist)

        # Now find the triplets in here.
        action_idx_arr = np.array([
            d['action_idx'] for d in batch_online_contrastive_data['info']
        ], dtype=np.int32)
        gt_pos_arr = np.array([
            d['delta_pose'] for d in batch_online_contrastive_data['info']
        ])

        # Take m = [2, 1, 2, 3] and run the below code.
        # (m * (m[None, :] == m[:, None]))[:, None, :] == m[:, None]  
        # Add 1 to remove any 0's 
        idx_arr = (action_idx_arr + 1)  
        action_label_mask = idx_arr * (idx_arr[None, :] == idx_arr[:, None])
        # label_mask (i,j,k) is True iff idx_arr[i] == idx_arr[j] == idx_arr[k]
        action_label_mask = action_label_mask[:, None, :] == idx_arr[:, None]

        gt_pos_dist = get_euclid_dist_matrix_for_data(gt_pos_arr[:, :3])

        # Get masked distances
        # masked_gt_pos_dist = gt_pos_dist * sim_action_labels
        # masked_emb_dist = emb_dist * sim_action_labels

        gt_dist_diff = gt_pos_dist[:, :, None] - gt_pos_dist[:, None, :]
        gt_dist_diff = gt_dist_diff * action_label_mask
        gt_margin = self.config.args.contrastive_gt_pose_margin
        gt_triplet_idxs = np.where(-gt_dist_diff > gt_margin)

        emb_margin = self.contrastive_margin
        emb_dist_diff = emb_dist[:, :, None] - emb_dist[:, None, :]
        emb_dist_diff = emb_dist_diff * action_label_mask
        emb_triplet_idxs = np.where(
            (emb_dist_diff > 1e-4) & (-emb_dist_diff < emb_margin))

        # Take an & over gt_dist_diff and emb_dist_diff and use those indexes.
        if sample_hard_examples_only:
            valid_triplet_idxs = np.where((-gt_dist_diff > gt_margin) &
                                        ((-emb_dist_diff < emb_margin) &
                                        (emb_dist_diff > 1e-4)))
        elif sample_all_examples:
            valid_triplet_idxs = np.where((-gt_dist_diff > gt_margin))
        else:
            raise ValueError("Unclear which triplets to sample.")
            

        # Now get the actual samples?
        num_triplets = valid_triplet_idxs[0].shape[0]
        if num_triplets > 0:
            valid_triplet_idxs_arr = np.vstack(valid_triplet_idxs).transpose()
        else:
            return None

        self.log_sampled_contrastive_examples(
            contrastive_data_log_idx,   
            batch_online_contrastive_data,
            valid_triplet_idxs_arr,
            action_idx_arr,
            gt_pos_arr,
            gt_pos_dist,
            all_emb,
            emb_dist)


        return valid_triplet_idxs_arr

    def log_sampled_contrastive_examples(self, 
                                         contrastive_data_log_idx,   
                                         batch_online_contrastive_data,
                                         triplet_idxs,
                                         action_idx_arr,
                                         gt_pose_arr,
                                         gt_pose_matrix,
                                         pred_emb_arr,
                                         pred_emb_dist_matrix):
        '''Save some of the sampled contrasive examples.'''
        all_action_idxs = np.unique(action_idx_arr)
        data_by_action_idx = {}

        def _get_data_contrastive_batch(idx):
            info_dict = batch_online_contrastive_data['info'][idx]


        for action in all_action_idxs:
            action_mask = action_idx_arr == action
            action_mask_idx = np.where(action_mask)
    
        max_log_samples = 512
        max_triplets = triplet_idxs.shape[0]
        for i in range(max_log_samples):
            trip_i = np.random.randint(max_triplets)
            # triplet is length 3 array with (anchor, same, diff)
            triplet = triplet_idxs[trip_i]
            anchor_idx, same_idx, diff_idx = triplet[0], triplet[1], triplet[2]
            anchor_data = batch_online_contrastive_data['info'][anchor_idx]
            same_data = batch_online_contrastive_data['info'][same_idx]
            diff_data = batch_online_contrastive_data['info'][diff_idx]

            triplet_data = [anchor_data, same_data, diff_data]
            
            triplet_data_dict = {
                'path': [t['path'] for t in triplet_data],
                'delta_pose': [t['delta_pose'] for t in triplet_data],
                'action': [t['action'] for t in triplet_data],
                'action_idx': [t['action_idx'] for t in triplet_data],
            }
            assert anchor_data['action_idx'] == same_data['action_idx']
            assert anchor_data['action_idx'] == diff_data['action_idx']

            D = gt_pose_matrix
            a, b, c = anchor_idx, same_idx, diff_idx
            triplet_data_dict['gt_pose_dist'] = \
                [D[a, a], D[a, b], D[a, c], 
                 D[b, a], D[b, b], D[b, c],
                 D[c, a], D[c, b], D[c, c]]

            assert (D[a, c] - D[a, b]) > self.config.args.contrastive_gt_pose_margin

            D = pred_emb_dist_matrix
            triplet_data_dict['emb_dist'] = \
                [D[a, a], D[a, b], D[a, c], 
                 D[b, a], D[b, b], D[b, c],
                 D[c, a], D[c, b], D[c, c]]
                
            if data_by_action_idx.get(anchor_data['action_idx']) is None:
                data_by_action_idx[anchor_data['action_idx']] = []

            data_by_action_idx[anchor_data['action_idx']].append(
                triplet_data_dict)
        
        # Now save this dict
        data_pkl_path = os.path.join(args.result_dir, 'contrastive_data', 
                                     '{}_data.pkl'.format(contrastive_data_log_idx))
        if not os.path.exists(os.path.dirname(data_pkl_path)):
            os.makedirs(os.path.dirname(data_pkl_path))
        
        with open(data_pkl_path, 'wb') as data_f:
            pickle.dump(data_by_action_idx, data_f, protocol=2)
            print("Did save sampled contrastive data: {}".format(data_pkl_path))

        return data_by_action_idx
    
    def save_emb_to_result_dict(self, result_dict, batch_result_dict, 
                                batch_data):
        for b in range(len(batch_data)):
            for k in ['path', 'info', 'action']:
                result_dict['data_info'][k].append(batch_data[b][k])

            if len(batch_result_dict['img_emb']) > 0:
                result_dict['emb']['img_emb'].append(
                    batch_result_dict['img_emb'][b].detach().cpu().numpy())
                result_dict['emb']['img_action_emb'].append(
                    batch_result_dict['img_action_emb'][b].detach().cpu().numpy())

            if batch_result_dict.get('pos_gt') is not None:
                result_dict['output']['pos_gt'].append(
                    batch_result_dict['pos_gt'][b])
                result_dict['output']['pos_pred'].append(
                    batch_result_dict['pos_pred'][b])
            
            if batch_result_dict.get('orient') is not None:
                for k in ['gt', 'pred']:
                    result_dict['output']['orient'][k].append(
                        batch_result_dict['orient'][k][b])

        num_data = len(result_dict['data_info']['path'])
        for k in ['pred_l1_error', 'pred_l2_error']:
            if batch_result_dict.get(k) is not None:
                if result_dict['output'][k] is None:
                    result_dict['output'][k] = batch_result_dict[k]
                else:
                    result_dict['output'][k] += batch_result_dict[k]
                
                result_dict['avg_{}'.format(k)] = result_dict['output'][k]/num_data
        for out_k in ['orient']:
            if batch_result_dict.get(out_k) is None:
                continue
            for in_k in ['pred_l1_error', 'pred_l2_error']:
                result_dict['output'][out_k][in_k] += batch_result_dict[out_k][in_k]
                result_dict['output'][out_k]['avg_'+in_k] = \
                    (result_dict['output'][out_k][in_k] / num_data)

        # Also get avg error in prediction for cases where gt > 0 only.
        orient_res_dict = result_dict['output']['orient']
        for b in range(len(batch_data)):
            orient_gt = batch_result_dict['orient']['gt'][b]
            orient_pred = batch_result_dict['orient']['pred'][b]
            # import ipdb; ipdb.set_trace()
            for axes in range(3):
                if abs(orient_gt[axes]) > 0.001:
                    orient_res_dict['nnz_pred_l1_error'][axes] += \
                        abs(orient_pred[axes] - orient_gt[axes])
                    orient_res_dict['nnz_pred_data_count'][axes] += 1
        if np.all(orient_res_dict['nnz_pred_data_count'] > 0):
            orient_res_dict['avg_nnz_pred_l1_error'] = \
                orient_res_dict['nnz_pred_l1_error']/orient_res_dict['nnz_pred_data_count']
            orient_avg_nnz_pred_l1_error = orient_res_dict['avg_nnz_pred_l1_error']
        else:
            orient_avg_nnz_pred_l1_error = 'n/a'
        
        assert num_data > 0
        print(bcolors.c_cyan("====> Total Error\n" 
                             "   \t     position: L1: {}, L2: {}\n"
                             "   \t       orient: L1: {}, L2: {}\n"
                             "   \t   nnz orient: L1: {}, count: {}, total: {}\n".format(
            array_fmt(result_dict['output']['pred_l1_error']/num_data, 6),
            array_fmt(result_dict['output']['pred_l2_error']/num_data, 6),
            array_fmt(orient_res_dict['pred_l1_error']/num_data, 6),
            array_fmt(orient_res_dict['pred_l2_error']/num_data, 6),
            orient_avg_nnz_pred_l1_error,
            orient_res_dict['nnz_pred_data_count'],
            num_data,
        )))

    def train(self, train=True, viz_images=False, save_embedding=True,
              log_prefix=''):
        print("Begin training")
        args = self.config.args
        log_freq_iters = args.log_freq_iters if train else 10
        dataloader = self.dataloader
        device = self.config.get_device()
        train_data_size = dataloader.get_data_size(train)

        online_contrastive_freq = 1

        # Reset log counter
        train_step_count, test_step_count = 0, 0
        contrastive_step_count, contrastive_data_log_idx = 0, 0
        for model in self.get_all_model_dict().values():
            model.to(device)
        # Switch off the norm layers in ResnetEncoder

        result_dict = {
            'emb': {
                'img_emb': [],
                'img_action_emb': [],
            },
            'data_info': {
                'path': [],
                'info': [],
                'action': [],
            },
            'train_info': {
                'train_output': {
                    'orient_gt_th_error': np.array([0., 0., 0.]),
                    'orient_gt_th_count': np.array([0, 0, 0], dtype=np.int32),
                    'torque_gt_th_error': np.array([0., 0., 0.]),
                    'torque_gt_th_count': np.array([0, 0, 0], dtype=np.int32),
                    'orient_lt_th_error': np.array([0., 0., 0.]),
                    'orient_lt_th_count': np.array([0, 0, 0], dtype=np.int32),
                }, 
                'test_output': {
                    'orient_gt_th_error': np.array([0., 0., 0.]),
                    'orient_gt_th_count': np.array([0, 0, 0], dtype=np.int32),
                    'torque_gt_th_error': np.array([0., 0., 0.]),
                    'torque_gt_th_count': np.array([0, 0, 0], dtype=np.int32),
                    'orient_lt_th_error': np.array([0., 0., 0.]),
                    'orient_lt_th_count': np.array([0, 0, 0], dtype=np.int32),
                },
            },
            'output': {
                'pos_gt': [],
                'pos_pred': [],
                'pred_l1_error': None,
                'pred_l2_error': None,
                'avg_pred_l1_error': None,
                'avg_pred_l2_error': None,

                'orient': {
                    'gt': [],
                    'pred': [],
                    'pred_l1_error': np.array([0., 0., 0.]),
                    'pred_l2_error': np.array([0., 0., 0.]),
                    'avg_pred_l1_error': None,
                    'avg_pred_l2_error': None,
                    'nnz_pred_l1_error': np.array([0., 0., 0.]),
                    'nnz_pred_data_count': np.zeros(3, dtype=np.int32),
                    'avg_nnz_pred_l1_error': None,
                }
            },
            'conf': {
                'train_x': [],
                'test_x': [],
                'train_y': [],
                'test_y': [],
            }
        }
        num_epochs = args.num_epochs if train else 1
        if train:
            for model in self.get_all_model_dict().values():
                model.train()
        else:
            for model in self.get_all_model_dict().values():
                model.eval()

        for e in range(num_epochs):
            dataloader.reset_batch_sampler(train)

            batch_size = args.batch_size
            num_batches = train_data_size // batch_size
            if train_data_size % batch_size != 0:
                num_batches += 1

            # Reset data items that we need to collect during training.
            if args.loss_type == 'classif':
                n_classes = args.classif_num_classes
                for conf_key in ['train_x', 'train_y']:
                    result_dict['conf'][conf_key].append(
                        np.zeros((n_classes, n_classes), dtype=np.int32))
                        
            for k in ['orient_gt_th_error', 'orient_gt_th_count',
                      'orient_lt_th_error', 'orient_lt_th_count',
                      'torque_gt_th_error', 'torque_gt_th_count']:
                result_dict['train_info']['train_output'][k][:] = 0

            for batch_idx in range(num_batches):
                # Get raw data from the dataloader.
                batch_data = []

                # for b in range(batch_size):
                batch_get_start_time = time.time()

                batch_data_idx_list = dataloader.get_train_idx_for_batch(batch_size, train)
                for data_idx in batch_data_idx_list:
                    data = dataloader.get_train_data_at_idx(data_idx, train=train)
                    batch_data.append(data)

                batch_get_end_time = time.time()
                # print("Data time: {:.4f}".format(
                    # batch_get_end_time - batch_get_start_time))

                # Process raw batch data
                proc_data_start_time = time.time()
                x_dict = self.process_raw_batch_data(batch_data)
                # Now collate the batch data together
                x_tensor_dict = self.collate_batch_data_to_tensors(x_dict)
                proc_data_end_time = time.time()

                run_batch_start_time = time.time()
                batch_result_dict = self.run_model_on_batch(
                    x_tensor_dict,
                    batch_size,
                    train=train,
                    save_preds=True)
                run_batch_end_time = time.time()

                # print("Batch get: {:4f}   \t  proc data: {:.4f}  \t  run: {:.4f}".format(
                    # batch_get_end_time - batch_get_start_time,
                    # proc_data_end_time - proc_data_start_time,
                    # run_batch_end_time - run_batch_start_time
                # ))
                if args.loss_type == 'classif':
                    result_dict['conf']['train_x'][-1] += batch_result_dict['conf_x']
                    result_dict['conf']['train_y'][-1] += batch_result_dict['conf_y']

                if args.loss_type == 'regr': 
                    for k in (('orient_gt_th_error', 'error'),
                              ('orient_gt_th_count', 'count'),
                              ('orient_lt_th_error', 'lt_error'),
                              ('orient_lt_th_count', 'lt_count')):
                        result_dict['train_info']['train_output'][k[0]] += \
                            batch_result_dict['orient_gt_th'][k[1]]

                    if args.use_ft_sensor:
                        result_dict['train_info']['train_output']['torque_gt_th_error'] += \
                            batch_result_dict['torque_gt_th']['error']
                        result_dict['train_info']['train_output']['torque_gt_th_count'] += \
                            batch_result_dict['torque_gt_th']['count']

                if not train and save_embedding:
                    self.save_emb_to_result_dict(result_dict, 
                                                 batch_result_dict,
                                                 batch_data)

                self.print_train_update_to_console(
                    e, num_epochs, batch_idx, num_batches,
                    train_step_count, batch_result_dict, train=train)

                plot_images = viz_images and train \
                    and train_step_count % log_freq_iters == 0
                plot_loss = train \
                    and train_step_count % args.print_freq_iters == 0

                if train:
                    self.plot_train_update_to_tensorboard(
                        x_dict, x_tensor_dict, batch_result_dict,
                        train_step_count,
                        plot_loss=plot_loss,
                        plot_images=plot_images,
                        log_prefix='/train/'
                    )

                if train:
                    if train_step_count % log_freq_iters == 0:
                        self.log_model_to_tensorboard(train_step_count)

                    # Save trained models
                    if train_step_count % args.save_freq_iters == 0:
                        self.save_checkpoint(train_step_count)
                    
                    # Run current model on val/test data.
                    if train_step_count % args.test_freq_iters == 0:
                        # Remove old stuff from memory
                        x_dict = None
                        x_tensor_dict = None
                        batch_result_dict = None
                        torch.cuda.empty_cache()

                        dataloader.reset_batch_sampler(train=False)
                        test_batch_size = args.batch_size
                        test_data_size = self.dataloader.get_data_size(
                            train=False)
                        num_batch_test = test_data_size // test_batch_size
                        if test_data_size % test_batch_size != 0:
                            num_batch_test += 1

                        test_data_idx, total_test_loss = 0, 0
                        total_pred_l1_error = np.zeros(3)
                        total_pred_l2_error = np.zeros(3)
                        total_orient_pred_l1_error = np.zeros(3)
                        total_orient_pred_l2_error = np.zeros(3)

                        for model in self.get_all_model_dict().values():
                            model.eval()

                        n_classes = args.classif_num_classes
                        for conf_key in ['test_x', 'test_y']:
                            result_dict['conf'][conf_key].append(
                                np.zeros((n_classes, n_classes), dtype=np.int32))
                        for k in ['orient_gt_th_error', 'orient_gt_th_count', 
                                  'orient_lt_th_error', 'orient_lt_th_count',
                                  'torque_gt_th_error', 'torque_gt_th_count']:
                            result_dict['train_info']['test_output'][k][:] = 0

                        print(bcolors.c_red("==== Test begin ==== "))
                        total_test_data_path = []
                        for test_e in range(num_batch_test):
                            batch_data = []

                            batch_data_idx_list = dataloader.get_train_idx_for_batch(
                                batch_size, train=False)
                            for data_idx in batch_data_idx_list:
                                data = dataloader.get_train_data_at_idx(
                                    data_idx, train=False)
                                batch_data.append(data)
                                total_test_data_path.append(data['path'])

                            # Process raw batch data
                            x_dict = self.process_raw_batch_data(batch_data)
                            # Now collate the batch data together
                            x_tensor_dict = self.collate_batch_data_to_tensors(
                                x_dict)
                            with torch.no_grad():
                                batch_result_dict = self.run_model_on_batch(
                                    x_tensor_dict, test_batch_size, train=False,
                                    save_preds=True)
                                total_test_loss += batch_result_dict['total_loss']
                                total_pred_l1_error += batch_result_dict['pred_l1_error']
                                total_pred_l2_error += batch_result_dict['pred_l2_error']
                                total_orient_pred_l1_error += batch_result_dict['orient']['pred_l1_error']
                                total_orient_pred_l2_error += batch_result_dict['orient']['pred_l2_error']

                                result_dict['output']['orient']['gt'].append(
                                    batch_result_dict['orient']['gt'])
                                result_dict['output']['orient']['pred'].append(
                                    batch_result_dict['orient']['pred'])

                                for k in (('orient_gt_th_error', 'error'),
                                          ('orient_gt_th_count', 'count'),
                                          ('orient_lt_th_error', 'lt_error'),
                                          ('orient_lt_th_count', 'lt_count')):
                                    result_dict['train_info']['test_output'][k[0]] += \
                                        batch_result_dict['orient_gt_th'][k[1]]

                                if args.use_ft_sensor:
                                    result_dict['train_info']['test_output']['torque_gt_th_error'] += \
                                        batch_result_dict['torque_gt_th']['error']
                                    result_dict['train_info']['test_output']['torque_gt_th_count'] += \
                                        batch_result_dict['torque_gt_th']['count']

                            if args.loss_type == 'classif':
                                result_dict['conf']['test_x'][-1] += \
                                    batch_result_dict['conf_x']
                                result_dict['conf']['test_y'][-1] += \
                                    batch_result_dict['conf_y']

                            self.print_train_update_to_console(
                                e, num_epochs, test_e, num_batch_test,
                                train_step_count, batch_result_dict, train=False)

                            plot_images = test_e == 0
                            plot_loss = True
                            self.plot_train_update_to_tensorboard(
                                x_dict, x_tensor_dict, batch_result_dict,
                                test_step_count,
                                plot_loss=plot_loss,
                                plot_images=plot_images,
                                log_prefix='/test/'
                            )

                            test_step_count += 1
                        print(bcolors.c_red("==== Test end ==== "))

                        # Save gt and preds
                        gt_preds = {
                            'gt': np.concatenate(result_dict['output']['orient']['gt']),
                            'pred': np.concatenate(result_dict['output']['orient']['pred']),
                            'path': total_test_data_path,
                        }
                        test_preds_path = os.path.join(args.result_dir, 'test_preds.pkl')
                        with open(test_preds_path, 'wb') as test_f:
                            pickle.dump(gt_preds, test_f, protocol=2)
                            print(f"Did save test gt and preds: {test_preds_path}")

                        # Plot the total loss on the entire dataset. Hopefull,
                        # this would decrease over time.
                        self.logger.summary_writer.add_scalar(
                            '/test/all_batch_loss/loss_avg',
                            total_test_loss / max(num_batch_test, 1),
                            test_step_count)
                        self.logger.summary_writer.add_scalar(
                            '/test/all_batch_loss/loss',
                            total_test_loss,
                            test_step_count)
                        
                        total_pred_l1_error = total_pred_l1_error/test_data_size
                        total_pred_l2_error = total_pred_l2_error/test_data_size
                        total_orient_pred_l1_error = total_orient_pred_l1_error/test_data_size
                        total_orient_pred_l2_error = total_orient_pred_l2_error/test_data_size
                        for dim in zip([0, 1, 2], ['x', 'y', 'z']):
                            self.logger.summary_writer.add_scalar(
                                '/test/avg_pred_l1_error/'+dim[1],
                                total_pred_l1_error[dim[0]],
                                test_step_count)
                            self.logger.summary_writer.add_scalar(
                                '/test/avg_pred_l2_error/'+dim[1],
                                total_pred_l2_error[dim[0]],
                                test_step_count)
                        orient_gt_th_error = \
                            result_dict['train_info']['test_output']['orient_gt_th_error']
                        orient_gt_th_count = \
                            result_dict['train_info']['test_output']['orient_gt_th_count']
                        orient_lt_th_error = \
                            result_dict['train_info']['test_output']['orient_lt_th_error']
                        orient_lt_th_count = \
                            result_dict['train_info']['test_output']['orient_lt_th_count']
                        torque_gt_th_error = \
                            result_dict['train_info']['test_output']['torque_gt_th_error']
                        torque_gt_th_count = \
                            result_dict['train_info']['test_output']['torque_gt_th_count']
                        test_ouput_dict = result_dict['train_info']

                        if args.weight_orient_contrastive_loss > 0.001:
                            assert np.all(orient_gt_th_count) > 0, "No data with orient change > th"
                        if args.use_ft_sensor:
                            assert np.all(torque_gt_th_count) > 0, "No data with torque change > th"
                        for dim in zip([0, 1, 2], ['theta_x', 'theta_y', 'theta_z']):
                            self.logger.summary_writer.add_scalar(
                                '/test/avg_pred_l1_error/'+dim[1],
                                total_orient_pred_l1_error[dim[0]],
                                test_step_count)
                            self.logger.summary_writer.add_scalar(
                                '/test/avg_pred_l2_error/'+dim[1],
                                total_orient_pred_l2_error[dim[0]],
                                test_step_count)

                            self.logger.summary_writer.add_scalar(
                                '/test/avg_orient_gt_th_error/'+dim[1],
                                orient_gt_th_error[dim[0]]/orient_gt_th_count[dim[0]],
                                test_step_count)
                            self.logger.summary_writer.add_scalar(
                                '/test/avg_orient_gt_th_count/'+dim[1],
                                orient_gt_th_count[dim[0]],
                                test_step_count)
                            self.logger.summary_writer.add_scalar(
                                '/test/avg_orient_lt_th_error/'+dim[1],
                                orient_lt_th_error[dim[0]]/orient_lt_th_count[dim[0]],
                                test_step_count)
                            self.logger.summary_writer.add_scalar(
                                '/test/avg_orient_lt_th_count/'+dim[1],
                                orient_lt_th_count[dim[0]],
                                test_step_count)

                            if args.use_ft_sensor:
                                self.logger.summary_writer.add_scalar(
                                    '/test/avg_torque_gt_th_error/'+dim[1],
                                    torque_gt_th_error[dim[0]]/torque_gt_th_count[dim[0]],
                                    test_step_count)
                                self.logger.summary_writer.add_scalar(
                                    '/test/avg_torque_gt_th_count/'+dim[1],
                                    torque_gt_th_count[dim[0]],
                                    test_step_count)
                        

                        print(' ==== Test Epoch conf start ====')
                        if args.loss_type == 'classif':
                            for k in ['test_x', 'test_y']:
                                print(bcolors.c_yellow('conf {}: \n{}'.format(
                                    k,
                                    array_fmt(result_dict['conf'][k][-1], 0))))
                        else:
                            print(bcolors.c_yellow(
                                "Avg error:  \t    pos L1:        \t  {}\n"
                                 "           \t    pos L2:        \t  {}\n"
                                 "           \t orient L1:        \t  {}\n"
                                 "           \t orient L2:        \t  {}\n"
                                 "           \t orient gt th:     \t  {}\n"
                                 "           \t orient gt count:  \t  {}\n"
                                 "           \t orient lt th:     \t  {}\n"
                                 "           \t orient lt count:  \t  {}".format(
                                    array_fmt(total_pred_l1_error, 6),
                                    array_fmt(total_pred_l2_error, 6),
                                    array_fmt(total_orient_pred_l1_error, 6),
                                    array_fmt(total_orient_pred_l2_error, 6),
                                    array_fmt(orient_gt_th_error/orient_gt_th_count, 6),
                                    array_fmt(orient_gt_th_count, 0),
                                    array_fmt(orient_lt_th_error/orient_lt_th_count, 6),
                                    array_fmt(orient_lt_th_count, 0))))
                            if args.use_ft_sensor:
                                print(bcolors.c_yellow(
                                    "           \t torque gt th:     \t  {}\n"
                                    "           \t torque gt count:  \t  {}\n".format(
                                        array_fmt(torque_gt_th_error/torque_gt_th_count, 6),
                                        array_fmt(torque_gt_th_count, 0))))
                        print(' ==== Test Epoch conf end ====')

                    x_dict = None
                    x_tensor_dict = None
                    batch_result_dict = None
                    torch.cuda.empty_cache()
                    if train:
                        for model in self.get_all_model_dict().values():
                            model.train()

                train_step_count += 1
                torch.cuda.empty_cache()

            # Take lr scheduler step 
            if train:
                self.lr_scheduler.step()
                if args.use_contact_preds:
                    self.contact_opt.step()
                if args.use_ft_sensor:
                    self.ft_sensor_lr_scheduler.step()

            if args.loss_type == 'classif':
                for k in ['train_x', 'train_y']:
                    print('conf {}: \n{}'.format(k,
                        array_fmt(result_dict['conf'][k][-1], 0)))

            orient_gt_th_error = \
                result_dict['train_info']['train_output']['orient_gt_th_error']
            orient_gt_th_count = \
                result_dict['train_info']['train_output']['orient_gt_th_count']
            orient_lt_th_error = \
                result_dict['train_info']['train_output']['orient_lt_th_error']
            orient_lt_th_count = \
                result_dict['train_info']['train_output']['orient_lt_th_count']
            torque_gt_th_error = \
                result_dict['train_info']['train_output']['torque_gt_th_error']
            torque_gt_th_count = \
                result_dict['train_info']['train_output']['torque_gt_th_count']
            for dim in zip([0, 1, 2], ['theta_x', 'theta_y', 'theta_z']):
                self.logger.summary_writer.add_scalar(
                    '/train/avg_orient_gt_th_error/'+dim[1],
                    orient_gt_th_error[dim[0]]/orient_gt_th_count[dim[0]],
                    train_step_count)
                self.logger.summary_writer.add_scalar(
                    '/train/avg_orient_gt_th_count/'+dim[1],
                    orient_gt_th_count[dim[0]],
                    train_step_count)
                self.logger.summary_writer.add_scalar(
                    '/train/avg_orient_lt_th_error/'+dim[1],
                    orient_lt_th_error[dim[0]]/orient_lt_th_count[dim[0]],
                    train_step_count)
                self.logger.summary_writer.add_scalar(
                    '/train/avg_orient_lt_th_count/'+dim[1],
                    orient_lt_th_count[dim[0]],
                    train_step_count)
                if args.use_ft_sensor:
                    self.logger.summary_writer.add_scalar(
                        '/train/avg_torque_gt_th_error/'+dim[1],
                        torque_gt_th_error[dim[0]]/torque_gt_th_count[dim[0]],
                        train_step_count)
                    self.logger.summary_writer.add_scalar(
                        '/train/avg_torque_gt_th_count/'+dim[1],
                        torque_gt_th_count[dim[0]],
                        train_step_count)

            debug_str = "orient gt_th error: {},  count: {} \t  less_th: {}, count: {}".format(
                array_fmt(orient_gt_th_error/orient_gt_th_count, 6),
                array_fmt(orient_gt_th_count, 0),
                array_fmt(orient_lt_th_error/orient_lt_th_count, 6),
                array_fmt(orient_lt_th_count, 0)
            )
            if args.use_ft_sensor:
                debug_str += "  \t  torque_gt_th error: {},    count: {}".format(
                    array_fmt(torque_gt_th_error/torque_gt_th_count, 6),
                    array_fmt(torque_gt_th_count, 0)
                )
            print(bcolors.c_cyan(debug_str))
            print(' ==== Epoch done ====')

        return result_dict

def update_test_args_with_train_args(test_args, train_args):
    assert train_args.z_dim == test_args.z_dim
    assert train_args.add_xy_channels == test_args.add_xy_channels
    assert train_args.use_bb_in_input == test_args.use_bb_in_input
    assert train_args.model == test_args.model
    assert train_args.loss_type == test_args.loss_type
    assert train_args.voxel_datatype == test_args.voxel_datatype
    assert train_args.use_ft_sensor == test_args.use_ft_sensor
    assert train_args.use_contact_preds == test_args.use_contact_preds

def create_voxel_trainer_with_checkpoint(checkpoint_path, cuda=False):
    dtype = torch.FloatTensor
    if cuda:
        dtype = torch.cuda.FloatTensor
    assert os.path.exists(checkpoint_path)

    result_dir = os.path.dirname(os.path.dirname(checkpoint_path))
    config_pkl_path = os.path.join(result_dir, 'config.pkl')
    with open(config_pkl_path, 'rb') as config_f:
        args = pickle.load(config_f)
        print(bcolors.c_red("Did load config: {}".format(config_pkl_path)))

        # Set some properties that might not exist in args
        # args.__dict__['model'] = 'simple_model'
        # args.__dict__['use_contact_preds'] = False 
        # args.__dict__['use_ft_sensor'] = False 
        # args.__dict__['use_l1_loss'] = True


    config = BaseVAEConfig(args, dtype=dtype)
    config.args.use_contrastive_loss = False
    trainer = VoxelRelationTrainer(config)
    trainer.load_checkpoint(checkpoint_path)
    return trainer


def create_model_with_checkpoint(model_name,
                                 checkpoint_path, 
                                 args=None, 
                                 cuda=False):
    '''Create model with checkpoint path and args loaded from the saved model
    dir.

    args: None if the args are none will try to load args from the emb cp dir.
    '''
    assert model_name in ['resnet10', 'resnet18', 'simple_model', 
                          'small_simple_model', 'bb_only'], \
        "Invalid model type"

    if args is None:
        result_dir = os.path.dirname(os.path.dirname(checkpoint_path))
        args_path = os.path.join(result_dir, 'config.pkl')
        with open(args_path, 'rb') as args_f:
            args = pickle.load(args_f)
            print(bcolors.c_green("Did load emb args: {}".format(args_path)))

    action_size = 7
    if model_name == 'resnet10':
        model = get_unscaled_resnet10(
            args.z_dim, action_size, args,
            use_spatial_softmax=args.use_spatial_softmax)
    elif model_name == 'resnet18':
        model = get_unscaled_resnet18(
            args.z_dim, action_size, args,
            use_spatial_softmax=args.use_spatial_softmax)
    elif model_name == 'simple_model':
        model = UnscaledVoxelModel(
            args.z_dim, action_size, args, n_classes=2*args.classif_num_classes,
            use_spatial_softmax=args.use_spatial_softmax,
            )
    elif model_name == 'small_simple_model':
        model = SmallEmbUnscaledVoxelModel(
            args.z_dim, action_size, args, n_classes=2*args.classif_num_classes,
            use_spatial_softmax=args.use_spatial_softmax,
            )
    elif model_name == 'bb_only':
        model = BoundingBoxOnlyModel(
            args.z_dim, action_size, args, n_classes=2*args.classif_num_classes,
            use_spatial_softmax=args.use_spatial_softmax)
    else:
        raise ValueError("Invalid model: {}".format(model))

    if checkpoint_path is not None and len(checkpoint_path) > 0:
        checkpoint_models = torch.load(checkpoint_path,
                                       map_location=torch.device('cpu'))
        model.load_state_dict(checkpoint_models['model'])

    return model


def main(args):
    dtype = torch.FloatTensor
    if args.cuda:
        dtype = torch.cuda.FloatTensor

    # Load the args from previously saved checkpoint
    if len(args.checkpoint_path) > 0:
        config_pkl_path = os.path.join(args.result_dir, 'config.pkl')
        with open(config_pkl_path, 'rb') as config_f:
            old_args = pickle.load(config_f)
            print(bcolors.c_red("Did load config: {}".format(config_pkl_path)))
        # Now update the current args with the old args for the train model
        update_test_args_with_train_args(args, old_args)

    config = BaseVAEConfig(args, dtype=dtype)
    create_log_dirs(config)

    trainer = VoxelRelationTrainer(config)

    if len(args.checkpoint_path) > 0:
        trainer.load_checkpoint(args.checkpoint_path)
        result_dict = trainer.train(train=False,
                                    viz_images=False,
                                    save_embedding=True)
        test_result_dir = os.path.join(
            os.path.dirname(args.checkpoint_path), '{}_result_{}'.format(
                args.cp_prefix, os.path.basename(args.checkpoint_path)[:-4]))
        if not os.path.exists(test_result_dir):
            os.makedirs(test_result_dir)
        emb_h5_path = os.path.join(test_result_dir, 'result_emb.h5')
        emb_h5_f = h5py.File(emb_h5_path, 'w')
        result_h5_dict = {'emb': result_dict['emb'],
                          'output': result_dict['output']}
        recursively_save_dict_contents_to_group(emb_h5_f, '/', result_h5_dict)
        emb_h5_f.flush()
        emb_h5_f.close()
        print("Did save emb: {}".format(emb_h5_path))

        pkl_path = os.path.join(test_result_dir, 'result_info.pkl')
        with open(pkl_path, 'wb') as pkl_f:
            result_pkl_dict = {
                'data_info': result_dict['data_info'],
                'output': result_dict['output']
                }
            pickle.dump(result_pkl_dict, pkl_f, protocol=2)
            print("Did save test info: {}".format(pkl_path))
    else:
        config_pkl_path = os.path.join(args.result_dir, 'config.pkl')
        config_json_path = os.path.join(args.result_dir, 'config.json')
        with open(config_pkl_path, 'wb') as config_f:
            pickle.dump((args), config_f, protocol=2)
            print(bcolors.c_red("Did save config: {}".format(config_pkl_path)))
        with open(config_json_path, 'w') as config_json_f:
            config_json_f.write(json.dumps(args.__dict__))

        # trainer.get_data_stats_for_classif()
        trainer.train(viz_images=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Train for edge classification')
    parser.add_argument('--model_type', type=str, default='vae_recons',
                        choices=['vae',
                                 'sg2im',
                                 'visual_rel_sg2im',
                                 'multi_object_visual_rel_sg2im'],
                        help='Model type to use.')
    add_common_args_to_parser(parser,
                              cuda=True,
                              result_dir=True,
                              checkpoint_path=True,
                              num_epochs=True,
                              batch_size=True,
                              lr=True,
                              save_freq_iters=True,
                              log_freq_iters=True,
                              print_freq_iters=True,
                              test_freq_iters=True)

    parser.add_argument('--train_dir', required=True, action='append',
                        help='Train dir.')
    parser.add_argument('--test_dir', required=True, action='append',
                        help='Test dir.')
    parser.add_argument('--cp_prefix', type=str, default='',
                        help='Prefix to be used to save embeddings.')

    parser.add_argument('--dataloader', type=str, default='simple', 
                        choices=['simple', 'orient_change_sampler'],
                        help='Dataloader to use.')
    parser.add_argument('--model', type=str, default='resnet', 
                        choices=['bb_only', 'simple_model', 'small_simple_model', 
                                 'resnet18', 'resnet10'],
                        help='Model to use.')
    parser.add_argument('--z_dim', type=int, default=128,
                        help='Embedding size to extract from image.')
    
    # Loss weights
    parser.add_argument('--scaled_mse_loss', type=str2bool, default=False,
                        help='Use scaled MSE loss in contrast to regular MSE.')
    parser.add_argument('--use_l1_loss', type=str2bool, default=False,
                        help='Use l1 loss.')
    parser.add_argument('--classif_num_classes', type=int, default=5,
                        help='Number of classes for classification.')
    parser.add_argument('--loss_type', type=str, default='regr',
                        choices=['classif', 'regr'],
                        help='Loss type to use')

    parser.add_argument('--weight_pos', type=float, default=1.0,
                        help='Weight for pos pred loss.')
    parser.add_argument('--weight_angle', type=float, default=1.0,
                        help='Weight for orientation change pred loss.')
    parser.add_argument('--weight_inv_model', type=float, default=1.0,
                        help='Weight for inverse model loss.')
    parser.add_argument('--weight_contrastive_loss', type=float, default=1.0,
                        help='Weight to use for contrastive loss.')
    parser.add_argument('--weight_orient_contrastive_loss', type=float, default=1.0,
                        help='Weight to use for orientation contrastive loss.')
    #  Add weights for force-torque sensor values
    parser.add_argument('--weight_ft_force', type=float, default=0.0,
                        help='Weight to use for fore part of FT sensor.')
    parser.add_argument('--weight_ft_torque', type=float, default=0.0,
                        help='Weight to use for torque part of FT sensor.')
    parser.add_argument('--weight_contact_mu', type=float, default=0.0,
                        help='Weight to use for contact mean prediction')
    parser.add_argument('--weight_contact_cov', type=float, default=0.0,
                        help='Weight to use for contact covariance prediction')

    parser.add_argument('--add_xy_channels', type=int, default=0,
                        choices=[0, 1],
                        help='0: no xy append, 1: xy append '
                             '2: xy centered on bb')
    parser.add_argument('--use_bb_in_input', type=int, default=1, choices=[0,1],
                        help='Use bb in input')
    # 0: sparse voxels that is the scene size is fixed and we have the voxels in there.
    # 1: dense voxels, such that the given scene is rescaled to fit the max size.
    parser.add_argument('--voxel_datatype', type=int, default=0,
                         choices=[0, 1],
                         help='Voxel datatype to use.')
    parser.add_argument('--octree_0_multi_thread', type=str2bool, default=0,
                         help='Use multithreading in octree 0.')
    parser.add_argument('--use_spatial_softmax', type=str2bool, default=False,
                         help='Use spatial softmax.')
    parser.add_argument('--save_full_3d', type=str2bool, default=False,
                        help='Save 3d voxel representation in memory.')
    parser.add_argument('--expand_voxel_points', type=str2bool, default=False,
                        help='Expand voxel points to internal points of obj.')

    parser.add_argument('--use_contrastive_loss', type=str2bool, default=False,
                        help='Use contrastive loss during training.')
    parser.add_argument('--contrastive_margin', type=float, default=1.0,
                        help='Margin to use in triplet loss.')
    parser.add_argument('--contrastive_gt_pose_margin', default=1.0, type=float,
                        help='GT pose margin for online contrastive loss')
    # Orientation contrastive loss args
    parser.add_argument('--use_orient_contrastive_loss', type=str2bool, 
                        default=False,
                        help='Use contrastive loss during training.')
    parser.add_argument('--orient_contrastive_margin', type=float, default=1.0,
                        help='Margin to use in triplet loss.')
    parser.add_argument('--orient_contrastive_gt_pose_margin', default=1.0, type=float,
                        help='GT pose margin for online contrastive loss')
    # Force-Torque args
    parser.add_argument('--use_ft_sensor', type=str2bool, default=False,
                        help='Predict mean forces experienced by FT sensor.')
    parser.add_argument('--use_contact_preds', type=str2bool, default=False,
                        help='Predict contacts distribution.')


    args = parser.parse_args()
    pprint.pprint(args.__dict__)
    np.set_printoptions(precision=4, linewidth=120)

    main(args)
