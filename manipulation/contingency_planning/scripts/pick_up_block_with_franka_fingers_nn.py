import argparse

import numpy as np
from autolab_core import YamlConfig, RigidTransform

from carbongym_utils.scene import GymScene
from carbongym_utils.assets import GymFranka, GymBoxAsset, GymURDFAsset
from carbongym_utils.math_utils import RigidTransform_to_transform, np_to_vec3, transform_to_np
from carbongym_utils.draw import draw_transforms

import keras
from keras.models import load_model

import tensorflow as tf
gpus = tf.config.experimental.list_physical_devices('GPU')
if gpus:
    try:
        for gpu in gpus:
            tf.config.experimental.set_memory_growth(gpu, True)

    except RuntimeError as e:
        print(e)

from scipy.spatial import distance
from sklearn.metrics import f1_score
from mpl_toolkits import mplot3d
import numpy as np
import matplotlib.pyplot as plt
import seaborn as sns

from sampling_methods import pick_random_skill_from_top_n, pick_most_uncertain, pick_top_skill
from franka_policies import PickUpBlockPolicy
from get_pan_pose import get_pan_pose
from random_utils import *

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--franka_cfg', '-fc', type=str, default='cfg/paper/franka_fingers.yaml')
    parser.add_argument('--block_cfg_dir', '-bcd', type=str, default='cfg/paper/baseline/')
    parser.add_argument('--block_urdf_dir', '-bud', type=str, default='assets/baseline/')
    parser.add_argument('--pan_cfg_dir', '-pcd', type=str, default='cfg/paper/pans/')
    parser.add_argument('--pan_urdf_dir', '-pud', type=str, default='assets/pans/')
    parser.add_argument('--inputs', '-i', type=str, default='data/franka_fingers_inputs.npy')
    parser.add_argument('--grasp_nn', type=str)# , default='baseline/pick_up/franka_fingers_pick_up_only_trained_model.h5')
    parser.add_argument('--contingency_nn_dir', type=str, default='same_blocks/pick_up/contingency_nns/')
    parser.add_argument('--block_num', '-b', type=int, default=0)
    parser.add_argument('--pan_num', '-p', type=int, default=0)
    parser.add_argument('--num_iterations', '-n', type=int, default=10)
    parser.add_argument('--data_dir', '-dd', type=str, default='baseline/pick_up/franka_fingers/')
    args = parser.parse_args()
    franka_cfg = YamlConfig(args.franka_cfg)
    block_cfg = YamlConfig(args.block_cfg_dir+'block'+str(args.block_num)+'.yaml')

    use_pan = False
    if args.pan_num > 0 and args.pan_num < 7:
        pan_cfg = YamlConfig(args.pan_cfg_dir+'pan'+str(args.pan_num)+'.yaml')
        use_pan = True

    success_success_contingency_nn = load_model(args.contingency_nn_dir + 'franka_fingers_success_success_contingency_model.h5')
    success_failure_contingency_nn = load_model(args.contingency_nn_dir + 'franka_fingers_success_failure_contingency_model.h5')
    failure_success_contingency_nn = load_model(args.contingency_nn_dir + 'franka_fingers_failure_success_contingency_model.h5')
    failure_failure_contingency_nn = load_model(args.contingency_nn_dir + 'franka_fingers_failure_failure_contingency_model.h5')

    scene = GymScene(franka_cfg['scene'])
    
    block = GymURDFAsset(block_cfg['block']['urdf_path'], 
                         scene.gym, scene.sim,
                        assets_root=args.block_urdf_dir,
                        asset_options=block_cfg['block']['asset_options'],
                        shape_props=[block_cfg['block']['block1']['shape_props'],
                                     block_cfg['block']['block2']['shape_props'],
                                     block_cfg['block']['block3']['shape_props']])
    table = GymBoxAsset(scene.gym, scene.sim, **franka_cfg['table']['dims'], 
                        shape_props=franka_cfg['table']['shape_props'], 
                        asset_options=franka_cfg['table']['asset_options'])
    franka = GymFranka(franka_cfg['franka'], scene.gym, scene.sim, actuation_mode='attractors')

    if use_pan:
        pan = GymURDFAsset(pan_cfg['pan']['urdf_path'], 
                           scene.gym, scene.sim,
                           assets_root=args.pan_urdf_dir,
                           asset_options=pan_cfg['pan']['asset_options'],
                           shape_props=pan_cfg['pan']['shape_props'])

    table_width = franka_cfg['table']['dims']['width']
    table_height = franka_cfg['table']['dims']['height']

    block_pose = RigidTransform_to_transform(RigidTransform(
        translation=[0.42, table_height + 0.1, 0.0]
    ))
    table_pose = RigidTransform_to_transform(RigidTransform(
        translation=[table_width/3, table_height/2, 0]
    ))
    franka_pose = RigidTransform_to_transform(RigidTransform(
        translation=[0, table_height + 0.01, 0],
        rotation=RigidTransform.quaternion_from_axis_angle([-np.pi/2, 0, 0])
    ))
    if use_pan:
        pan_pose = get_pan_pose(args.pan_num, table_height)
    
    scene.add_asset('table0', table, table_pose)
    scene.add_asset('franka0', franka, franka_pose)
    scene.add_asset('block0', block, block_pose, collision_filter=2)
    if use_pan:
        scene.add_asset('pan0', pan, pan_pose)

    def custom_draws(scene):
        for i, env_ptr in enumerate(scene.env_ptrs):
            ee_transform = franka.get_ee_transform(env_ptr, 'franka0')
            desired_ee_transform = franka.get_desired_ee_transform(i, 'franka0')

            draw_transforms(scene.gym, scene.viewer, [env_ptr], [ee_transform, desired_ee_transform])

    def reset(env_index, env_ptr):
        block.set_rb_transforms(env_ptr, scene.ah_map[i]['block0'], [block_pose, block_pose, block_pose])
        franka.set_joints(env_ptr, scene.ah_map[env_index]['franka0'], franka.INIT_JOINTS)

    n_envs = franka_cfg['scene']['n_envs']
    if args.inputs is None:
        if args.grasp_nn is None:
            num_simulations = int(np.ceil(args.num_iterations / n_envs))

            num_iterations = num_simulations * n_envs

            x_y_thetas = get_random_x_y_thetas(num_iterations)
        else:
            grasp_nn_model = load_model(args.grasp_nn)

            num_simulations = int(np.ceil(args.num_iterations * 50 / n_envs))

            num_iterations = num_simulations * n_envs

            x_y_thetas = get_random_x_y_thetas(num_iterations)

            probabilities = grasp_nn_model.predict(x_y_thetas)
            sorted_idx = np.argsort(-probabilities, 0)
            sorted_x_y_thetas = x_y_thetas[sorted_idx].reshape(-1,3)
            x_y_thetas = sorted_x_y_thetas[:args.num_iterations,:]
            np.save('same_blocks/pick_up/franka_fingers_inputs.npy', x_y_thetas)
    else:
        x_y_thetas = np.load(args.inputs)
        
        num_simulations = int(np.floor(x_y_thetas.shape[0] / n_envs))

        num_iterations = num_simulations * n_envs
        x_y_thetas = x_y_thetas[:num_iterations]
        num_skills = num_iterations
        num_simulations = args.num_iterations

    block_data = np.load(args.data_dir + 'successful_lift_block'+str(args.block_num)+'_pick_up_block_with_franka_fingers.npy')

    fig = plt.figure()
    ax = plt.axes(projection='3d')

    ax.scatter(x_y_thetas[:,0], x_y_thetas[:,1], c=block_data);
    ax.set_title('Surface plot')
    ax.set_xlabel('x (m)')
    ax.set_ylabel('y (m)')
    ax.set_zlabel('Num Samples')
    ax.view_init(azim=0, elev=90)
    plt.show()

    policy = PickUpBlockPolicy(franka, 'franka0', block, 'block0', n_envs, x_y_thetas)

    initial_block_pose = np.zeros((num_iterations,7))

    pre_grasp_contact_forces = np.zeros((num_iterations,3))
    pre_grasp_robot_pose = np.zeros((num_iterations,7))
    desired_pre_grasp_robot_pose = np.zeros((num_iterations,7))
    pre_grasp_block_pose = np.zeros((num_iterations,7))

    grasp_contact_forces = np.zeros((num_iterations,3))
    grasp_block_pose = np.zeros((num_iterations,7))
    
    post_grasp_contact_forces = np.zeros((num_iterations,3))
    post_grasp_block_pose = np.zeros((num_iterations,7))
    
    post_release_block_pose = np.zeros((num_iterations,7))
    
    def save_block_pose(scene, t_step, t_sim, simulation_num):

        if t_step == 50:
            for env_idx, env_ptr in enumerate(scene.env_ptrs):
                block_ah = scene.ah_map[env_idx]['block0']
                initial_block_pose[simulation_num * n_envs + env_idx] = transform_to_np(block.get_rb_transforms(env_ptr, block_ah)[1])
        elif t_step == 349:
            for env_idx, env_ptr in enumerate(scene.env_ptrs):
                franka_ah = scene.ah_map[env_idx]['franka0']
                pre_grasp_contact_forces[simulation_num * n_envs + env_idx] = franka.get_ee_ct_forces(env_ptr, franka_ah)
                desired_pre_grasp_robot_pose[simulation_num * n_envs + env_idx] = transform_to_np(franka.get_desired_ee_transform(env_idx, 'franka0'))
                pre_grasp_robot_pose[simulation_num * n_envs + env_idx] = transform_to_np(franka.get_ee_transform(env_ptr, 'franka0'))
                
                block_ah = scene.ah_map[env_idx]['block0']
                pre_grasp_block_pose[simulation_num * n_envs + env_idx] = transform_to_np(block.get_rb_transforms(env_ptr, block_ah)[1])
        elif t_step == 400:
            for env_idx, env_ptr in enumerate(scene.env_ptrs):
                franka_ah = scene.ah_map[env_idx]['franka0']
                grasp_contact_forces[simulation_num * n_envs + env_idx] = franka.get_ee_ct_forces(env_ptr, franka_ah)

                block_ah = scene.ah_map[env_idx]['block0']
                grasp_block_pose[simulation_num * n_envs + env_idx] = transform_to_np(block.get_rb_transforms(env_ptr, block_ah)[1])
        elif t_step == 549:
            for env_idx, env_ptr in enumerate(scene.env_ptrs):
                franka_ah = scene.ah_map[env_idx]['franka0']
                post_grasp_contact_forces[simulation_num * n_envs + env_idx] = franka.get_ee_ct_forces(env_ptr, franka_ah)

                block_ah = scene.ah_map[env_idx]['block0']
                post_grasp_block_pose[simulation_num * n_envs + env_idx] = transform_to_np(block.get_rb_transforms(env_ptr, block_ah)[1])
        elif t_step == 749:
            for env_idx, env_ptr in enumerate(scene.env_ptrs):
                block_ah = scene.ah_map[env_idx]['block0']
                post_release_block_pose[simulation_num * n_envs + env_idx] = transform_to_np(block.get_rb_transforms(env_ptr, block_ah)[1])
                

    skill_success_probabilities = np.ones(num_skills) * 0.5
    skill_failure_probabilities = np.ones(num_skills) * 0.5

    for simulation_num in range(num_simulations):
        print(simulation_num)

        #skill_num = pick_random_skill_from_top_n(skill_success_probabilities, int(num_skills * 0.02))
        #skill_num = pick_top_skill(skill_success_probabilities)

        if simulation_num < 3:
            skill_num = pick_most_uncertain(skill_success_probabilities)
        else:
            skill_num = pick_random_skill_from_top_n(skill_success_probabilities, int(num_skills * 0.02))

        current_input = x_y_thetas[skill_num]
        cur_x_y_thetas = np.repeat(current_input.reshape(1,-1), x_y_thetas.shape[0], axis=0)

        policy.reset()
        for i, env_ptr in enumerate(scene.env_ptrs):
            reset(i, env_ptr)
        policy.set_simulation_num(simulation_num)
        policy.set_x_y_thetas(cur_x_y_thetas)

        scene.run(time_horizon=policy.time_horizon, policy=policy, custom_draws=custom_draws, cb=(lambda scene, t_step, t_sim: save_block_pose(scene, t_step, t_sim, simulation_num=simulation_num)))

        xs = x_y_thetas[:,:2] - current_input[:2]
        thetas = np.arctan2(np.sin(x_y_thetas[:,2] - current_input[2]), np.cos(x_y_thetas[:,2] - current_input[2]))
        relative_transforms = np.hstack((xs, thetas.reshape(-1,1)))

        if (post_release_block_pose[simulation_num,1] - initial_block_pose[simulation_num,1] < 0.1 and
            post_grasp_block_pose[simulation_num,1] - initial_block_pose[simulation_num,1] > 0.1):
            new_skill_success_probabilities = success_success_contingency_nn.predict(relative_transforms).reshape(-1)
            new_skill_failure_probabilities = failure_success_contingency_nn.predict(relative_transforms).reshape(-1)
                
            # fig = plt.figure()
            # ax = plt.axes(projection='3d')

            # ax.scatter(relative_transforms[:,0], relative_transforms[:,1], c=new_skill_success_probabilities);
            # #ax.scatter(current_input[0], current_input[1], c='red');
            # ax.set_title('Surface plot')
            # ax.set_xlabel('x (m)')
            # ax.set_ylabel('y (m)')
            # ax.set_zlabel('Num Samples')
            # ax.view_init(azim=0, elev=90)
            # plt.show()
        else:
            new_skill_success_probabilities = success_failure_contingency_nn.predict(relative_transforms).reshape(-1)
            new_skill_failure_probabilities = failure_failure_contingency_nn.predict(relative_transforms).reshape(-1)

            # fig = plt.figure()
            # ax = plt.axes(projection='3d')

            # ax.scatter(relative_transforms[:,0], relative_transforms[:,1], c=new_skill_success_probabilities);
            # ax.scatter(current_input[0], current_input[1], c='red');
            # ax.set_title('Surface plot')
            # ax.set_xlabel('x (m)')
            # ax.set_ylabel('y (m)')
            # ax.set_zlabel('Num Samples')
            # ax.view_init(azim=0, elev=90)
            # plt.show()

        skill_success_probabilities = skill_success_probabilities * new_skill_success_probabilities
        skill_failure_probabilities = skill_failure_probabilities * new_skill_failure_probabilities

        skill_probabilities_sum = skill_success_probabilities + skill_failure_probabilities
        skill_success_probabilities = skill_success_probabilities / skill_probabilities_sum
        skill_failure_probabilities = skill_failure_probabilities / skill_probabilities_sum

        fig = plt.figure()
        ax = plt.axes(projection='3d')

        ax.scatter(x_y_thetas[:,0], x_y_thetas[:,1], c=skill_success_probabilities);
        ax.scatter(current_input[0], current_input[1], c='red');
        ax.set_title('Surface plot')
        ax.set_xlabel('x (m)')
        ax.set_ylabel('y (m)')
        ax.set_zlabel('Num Samples')
        ax.view_init(azim=0, elev=90)
        plt.show()

    # save_file_name = args.data_dir+'block'+str(args.block_num)+'_pick_up_block_with_franka_fingers.npz'
    # np.savez(save_file_name, x_y_thetas=x_y_thetas, 
    #                          initial_block_pose=initial_block_pose,
    #                          pre_grasp_contact_forces=pre_grasp_contact_forces,
    #                          pre_grasp_block_pose=pre_grasp_block_pose,
    #                          desired_pre_grasp_robot_pose=desired_pre_grasp_robot_pose,
    #                          pre_grasp_robot_pose=pre_grasp_robot_pose,
    #                          grasp_contact_forces=grasp_contact_forces,
    #                          grasp_block_pose=grasp_block_pose,
    #                          post_grasp_contact_forces=post_grasp_contact_forces,
    #                          post_grasp_block_pose=post_grasp_block_pose,
    #                          post_release_block_pose=post_release_block_pose)