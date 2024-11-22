import torch
import numpy as np
from control_scripts import get_pictures, get_frames, get_depth_frame_intrinsics
from config import n_depth_samples, realSenseFPS, tcp_Z_offset, topview_vec
import pyrealsense2 as rs
import matplotlib.pyplot as plt
import matplotlib.patches as patches
import open3d as o3d
import cv2
import warnings
from sklearn.decomposition import PCA
from magpie_control.ur5 import pose_vector_to_homog_coord, homog_coord_to_pose_vector

def rotation_matrix_to_rpy(R):
    roll = np.arctan2(R[2, 1], R[2, 2])  # atan2(R32, R33)
    pitch = -np.arcsin(R[2, 0])          # -asin(R31)
    yaw = np.arctan2(R[1, 0], R[0, 0])   # atan2(R21, R11)
    return roll, pitch, yaw
def rpy_to_rotation_matrix(roll, pitch, yaw):
    # Compute individual rotation matrices
    R_x = np.array([
        [1, 0, 0],
        [0, np.cos(roll), -np.sin(roll)],
        [0, np.sin(roll), np.cos(roll)]
    ])
    
    R_y = np.array([
        [np.cos(pitch), 0, np.sin(pitch)],
        [0, 1, 0],
        [-np.sin(pitch), 0, np.cos(pitch)]
    ])
    
    R_z = np.array([
        [np.cos(yaw), -np.sin(yaw), 0],
        [np.sin(yaw), np.cos(yaw), 0],
        [0, 0, 1]
    ])
    
    # Combine rotations: R = R_z * R_y * R_x
    R = R_z @ R_y @ R_x
    return R
def get_observation_patch(obs, edge_color = "r"):
    rect = patches.Rectangle(
                            (obs.xmin, obs.ymin),
                                obs.xmax - obs.xmin,
                                obs.ymax - obs.ymin,
                                linewidth=2, edgecolor=edge_color, facecolor='none'
                            )
    return rect
def get_refined_depth(rs_wrapper):
    warnings.simplefilter("ignore", category=RuntimeWarning)
    depth_images = []
    rgb_img = None
    for i in range(n_depth_samples):
        rgb_img, depth_image = get_pictures(rs_wrapper)
        depth_images.append(depth_image)

    depth_stack = np.stack(depth_images, axis=0)
    #print(f"{depth_stack.shape=}")

    #Compute mean ignoring 0 values
    sum_depth_stack = np.sum(depth_stack, axis=0)
    non_zero_counts = np.count_nonzero(depth_stack, axis=0)
    #print(f"{sum_depth_stack.shape=}")
    #print(f"{non_zero_counts.shape=}")
    mean_depth_image = sum_depth_stack/non_zero_counts#np.divide(sum_depth_stack, non_zero_counts, where=non_zero_counts != 0)
    mean_depth_image = np.nan_to_num(mean_depth_image, nan=0)
    #print(mean_depth_image.shape)

    #compute std deviation ignoring 0 values
    squared_diff_stack = (depth_stack - mean_depth_image[None, :, :]) ** 2
    squared_diff_stack[depth_stack == 0] = 0  # Ignore zero values
    sum_squared_diff = np.sum(squared_diff_stack, axis=0)
    std_dev_image = np.sqrt(sum_squared_diff / non_zero_counts)
    std_dev_image = np.nan_to_num(std_dev_image, nan=0)
    #print(f"{std_dev_image.shape=}")

    #get mask of points within 1 standard deviation
    lower_bounds = mean_depth_image - std_dev_image
    upper_bounds = mean_depth_image + std_dev_image
    mask = (depth_stack >= lower_bounds[None, :, :]) & (depth_stack <= upper_bounds[None, :, :])
    #set points not within one standard deviation to 0
    filtered_depth_stack = np.where(mask, depth_stack, 0)

    #Compute mean ignoring 0 values and values not within 1 standard deviation
    sum_depth_stack = np.sum(filtered_depth_stack, axis=0)
    non_zero_counts = np.count_nonzero(filtered_depth_stack, axis=0)
    filtered_depth_image = sum_depth_stack/non_zero_counts
    filtered_depth_image = filtered_depth_image.astype(np.float32)

    warnings.simplefilter("default", category=RuntimeWarning)
    return filtered_depth_image

class observation:
    def __init__(self, str_label, label_vit, sam_predictor):
        self.str_label = str_label
        self.label_vit = label_vit
        self.sam_predictor = sam_predictor

        self.xmin = None
        self.xmax = None
        self.ymin = None
        self.ymax = None
        
        self.mask = None
        
        self.rgb_segment = None
        self.depth_segment = None
        self.pcd = None
        self.pcd_bbox = None

        self.pickPose = None
        self.placePose = None
        
    def calc_bbox(self, rgb_img):
        bbox = None
        with torch.no_grad():
            bbox = self.label_vit.label(rgb_img, self.str_label, self.str_label, plot=False, topk=True)
            bbox = bbox[1][0].tolist()
        self.xmin = int(bbox[0])
        self.ymin = int(bbox[1])
        self.xmax = int(bbox[2])
        self.ymax = int(bbox[3])

        #self.xmin = np.clip(self.xmin, 0+invalid_border_px_x, rgb_img.shape[1]-invalid_border_px_x)
        #self.xmax = np.clip(self.xmax, 0+invalid_border_px_x, rgb_img.shape[1]-invalid_border_px_x)
        #self.ymin = np.clip(self.ymin, 0+invalid_border_px_y, rgb_img.shape[0]-invalid_border_px_y)
        #self.ymax = np.clip(self.ymax, 0+invalid_border_px_y, rgb_img.shape[0]-invalid_border_px_y)

    def calc_pc(self, rgb_img, depth_img, K, depth_scale, observation_pose):
        self.sam_predictor.set_image(rgb_img)
        sam_box = np.array([self.xmin,  self.ymin,  self.xmax,  self.ymax])
        sam_mask, sam_scores, sam_logits = self.sam_predictor.predict(box=sam_box)
        sam_mask = np.all(sam_mask, axis=0)
        #expanded_sam_mask = np.expand_dims(sam_mask, axis=-1)
        

        self.mask = sam_mask
        self.rgb_segment = rgb_img.copy()
        self.rgb_segment[~sam_mask] = 0
        self.depth_segment = depth_img.copy()
        self.depth_segment[~sam_mask] = 0

        temp_rgb_img = o3d.geometry.Image(self.rgb_segment)
        temp_depth_img = o3d.geometry.Image(self.depth_segment)

        rgbd = o3d.geometry.RGBDImage.create_from_color_and_depth(temp_rgb_img, temp_depth_img, depth_scale=depth_scale, depth_trunc=10.0, convert_rgb_to_intensity=False)
        #print(f"{dir(K)=}")
        intrinsic = o3d.camera.PinholeCameraIntrinsic(K.width, K.height, K.fx, K.fy, K.ppx, K.ppy)
        pcd = o3d.geometry.PointCloud.create_from_rgbd_image(rgbd, intrinsic)
        #pcd = pcd.uniform_down_sample(every_k_points=5)
        #pcd = pcd.voxel_down_sample(voxel_size=0.001)  # Down-sample with finer detail
        transform_matrix = pose_vector_to_homog_coord(observation_pose)
        pcd.transform(transform_matrix)
        
        
        #if self.pcd is None:
        self.pcd = pcd
        #else:
        #    self.pcd = self.pcd + pcd
        #self.pcd, _ = self.pcd.remove_statistical_outlier(nb_neighbors=50, std_ratio=1.0)
        self.pcd, _ = self.pcd.remove_statistical_outlier(nb_neighbors=1000, std_ratio=1.0)
        self.pcd_bbox = self.pcd.get_axis_aligned_bounding_box()
        self.pcd_bbox.color = (1,0,0)
        

        
    def calc_pick_pose(self):
        self.centroid = self.pcd_bbox.get_center()
        
        self.pickPose = topview_vec.copy()
        self.pickPose[0] = self.centroid[0]
        self.pickPose[1] = self.centroid[1]
        self.pickPose[2] = self.centroid[2] + tcp_Z_offset
        #self.pcd_bbox = pcd.get_oriented_bounding_box()
        #print(f"{dir(self.pcd_bbox)=}")

        
        #pca = PCA(n_components=3)
        #pca.fit(np.asarray(pcd.points) - self.centroid)
        

    def calc_place_pose(self):
        self.placePose = self.pickPose.copy()
        self.placePose[2] += 0.04

    def update_observation(self, rgb_img, depth_img, K, depth_scale,  observation_pose, display = False):
        self.calc_bbox(rgb_img)

        self.calc_pc(rgb_img, depth_img, K, depth_scale, observation_pose)

        self.calc_pick_pose()
        self.calc_place_pose()

        if display:
            fig, axes = plt.subplots(nrows=2, ncols=2)
            axes[0, 0].imshow(rgb_img)
            axes[0, 0].add_patch(get_observation_patch(self, "r"))
            axes[0, 0].text(self.xmin, self.ymin - 10, f"{self.str_label}", color='r', fontsize=12, ha='left', va='bottom')
            axes[0, 0].set_title("RGB Image")

            axes[1, 0].imshow(self.mask)
            axes[1, 0].set_title("Mask")
            
            axes[0, 1].imshow(self.rgb_segment)
            axes[0, 1].set_title("RGB segment")

            axes[1, 1].imshow(self.depth_segment)
            axes[1, 1].set_title("Depth segment")


            plt.tight_layout()
            #print(f"Showing observation for {self.str_label}")
            plt.show(block = False)
            plt.pause(1)  # Keeps the figure open for 3 seconds


class observation_manager:
    def __init__(self, things_to_observe, rs_wrapper, label_vit, sam_predictor, UR_interface):
        self.rs_wrapper = rs_wrapper
        self.label_vit = label_vit
        self.sam_predictor = sam_predictor
        self.UR_interface = UR_interface
        self.observation_pose = None

        self.observations = {}
        for thing in things_to_observe:
            self.observations[thing] = observation(thing, self.label_vit, self.sam_predictor)

    def update_observations(self, display = False):
        rgb_img, depth_img = get_pictures(self.rs_wrapper)
        depth_img = get_refined_depth(self.rs_wrapper)
        depth_scale, K = get_depth_frame_intrinsics(self.rs_wrapper)
        
        self.observation_pose = homog_coord_to_pose_vector(self.UR_interface.get_cam_pose())#self.UR_interface.recv.getActualTCPPose()
        print(f"{self.observation_pose=}")
        #print(f"{self.observation_pose=}")
        observation_list = list(self.observations.values())
        for obs in observation_list:
            #print(f"Updating observation for {obs.str_label}")
            obs.update_observation(rgb_img, depth_img, K, depth_scale, self.observation_pose)
        if display:
            self.display()

    def display(self):
        vis = o3d.visualization.Visualizer()
        vis.create_window()

        T = pose_vector_to_homog_coord(self.observation_pose)
        axis = o3d.geometry.TriangleMesh.create_coordinate_frame(size=0.05, origin=[0, 0, 0])
        axis.transform(T)
        vis.add_geometry(axis)

        for label, observation in self.observations.items():
            vis.add_geometry(observation.pcd)

            sphere = o3d.geometry.TriangleMesh.create_sphere(radius=0.005)
            sphere.translate(observation.centroid)
            sphere.paint_uniform_color([0, 0, 0])
            vis.add_geometry(sphere)

            #pickPose_axis = o3d.geometry.TriangleMesh.create_coordinate_frame(size=0.03, origin=[0, 0, 0])
            #rot_mat = rpy_to_rotation_matrix(observation.pickPose[3], observation.pickPose[4], observation.pickPose[5])
            #pickPose_axis.rotate(rot_mat, center=(0, 0, 0))
            #print(observation.pickPose[:3])
            #pickPose_axis.translate(observation.pickPose[:3])
            #vis.add_geometry(pickPose_axis)

            vis.add_geometry(observation.pcd_bbox)

        axis = o3d.geometry.TriangleMesh.create_coordinate_frame(size=0.2, origin=[0, 0, 0])    
        vis.add_geometry(axis)
        opt = vis.get_render_option()
        opt.point_size = 1.0  # Set to a smaller size (default is 5.0)
        # Run the visualizer
        vis.run()
        

if __name__ == "__main__":
    from magpie_control import realsense_wrapper as real
    from sam2.sam2_image_predictor import SAM2ImagePredictor
    from magpie_perception.label_owlv2 import LabelOWLv2
    from magpie_control.ur5 import UR5_Interface as robot
    from control_scripts import goto_vec
    from config import frontview_vec, leftview_vec, rightview_vec, behindview_vec

    myrobot = robot()
    print(f"starting robot from observation")
    myrobot.start()


    myrs = real.RealSense(fps=realSenseFPS)
    myrs.initConnection()

    label_vit = LabelOWLv2(topk=1, score_threshold=0.01, cpu_override=False)
    label_vit.model.eval()
    print(f"{label_vit.model.device=}")

    sam_predictor = SAM2ImagePredictor.from_pretrained("facebook/sam2-hiera-large")
    print(f"{sam_predictor.model.device=}")

    print(f"{topview_vec=}")

    observation_list = ["red block", "blue block", "green block", "yellow block", "white paper"]
    om = observation_manager(observation_list, myrs, label_vit, sam_predictor, myrobot)

    
    goto_vec(myrobot, frontview_vec)
    om.update_observations(display=False)
    """
    goto_vec(myrobot, leftview_vec)
    om.update_observations(display=False)

    goto_vec(myrobot, behindview_vec)
    om.update_observations(display=False)
    
    goto_vec(myrobot, rightview_vec)
    om.update_observations(display=False)
    """
    goto_vec(myrobot, topview_vec)
    om.update_observations(display=True)

    for target in observation_list[:-1]:
        target_pose = om.observations[target].pickPose.copy()
        target_pose[2] 
        #target_pose[3] = topview_vec[3]
        #target_pose[4] = topview_vec[4]
        #target_pose[5] = topview_vec[5]
        print(f"{om.observations[target].str_label}={target_pose}")
        
        goto_vec(myrobot, target_pose)
        input()
        goto_vec(myrobot, topview_vec)
    om.display()
    myrobot.stop()
    myrs.disconnect()
    