$source = "https://www.dropbox.com/s/7na5qsjzz8yfoer/cen_patches_0.25_of.dat?dl=1"
$destination = "model/patch_experts/cen_patches_0.25_of.dat"
Invoke-WebRequest $source -OutFile $destination
$source = "https://www.dropbox.com/s/k7bj804cyiu474t/cen_patches_0.35_of.dat?dl=1"
$destination = "model/patch_experts/cen_patches_0.35_of.dat"
Invoke-WebRequest $source -OutFile $destination
$source = "https://www.dropbox.com/s/ixt4vkbmxgab1iu/cen_patches_0.50_of.dat?dl=1"
$destination = "model/patch_experts/cen_patches_0.50_of.dat"
Invoke-WebRequest $source -OutFile $destination
$source = "https://www.dropbox.com/s/2t5t1sdpshzfhpj/cen_patches_1.00_of.dat?dl=1"
$destination = "model/patch_experts/cen_patches_1.00_of.dat"
Invoke-WebRequest $source -OutFile $destination