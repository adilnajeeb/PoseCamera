import argparse
import os

import cv2
import numpy as np
import torch
from werkzeug.utils import secure_filename

from models.with_mobilenet import PoseEstimationWithMobileNet
from modules.file_providers import ImageReader, VideoReader
from modules.keypoints import extract_keypoints, group_keypoints
from modules.load_state import load_state
from modules.pose import Pose, track_poses
from val import normalize, pad_width

from flask import Flask, request, render_template, json

app = Flask(__name__)
args =  None

UPLOAD_DIR = './tmp'

@app.route('/', methods=['GET', 'POST'])
def detect():
    global args
    if request.method == 'GET':
        return render_template('docs.html')
    else:
        image_files = []
        for name in request.files:
            file = request.files[name]
            file_path = os.path.join(UPLOAD_DIR, secure_filename(file.filename))

            file.save(file_path)
            image_files.append(file_path)

        pose_results = run_inference(net, ImageReader(image_files), args.height_size, args.cpu, args.track, args.smooth, args.no_display, True)
        pose_keypoints = [pose.keypoints.tolist() for pose in pose_results]

        return app.response_class(
            response=json.dumps(pose_keypoints),
            mimetype='application/json'
        )


def infer_fast(net, img, net_input_height_size, stride, upsample_ratio, cpu, 
               pad_value=(0, 0, 0), img_mean=(128, 128, 128), img_scale=1/256):
    height, width, _ = img.shape
    scale = net_input_height_size / height

    scaled_img = cv2.resize(img, (0, 0), fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    scaled_img = normalize(scaled_img, img_mean, img_scale)
    min_dims = [net_input_height_size, max(scaled_img.shape[1], net_input_height_size)]
    padded_img, pad = pad_width(scaled_img, stride, pad_value, min_dims)

    tensor_img = torch.from_numpy(padded_img).permute(2, 0, 1).unsqueeze(0).float()
    if not cpu:
        tensor_img = tensor_img.cuda()

    stages_output = net(tensor_img)

    stage2_heatmaps = stages_output[-2]
    heatmaps = np.transpose(stage2_heatmaps.squeeze().cpu().data.numpy(), (1, 2, 0))
    heatmaps = cv2.resize(heatmaps, (0, 0), fx=upsample_ratio, fy=upsample_ratio, interpolation=cv2.INTER_CUBIC)

    stage2_pafs = stages_output[-1]
    pafs = np.transpose(stage2_pafs.squeeze().cpu().data.numpy(), (1, 2, 0))
    pafs = cv2.resize(pafs, (0, 0), fx=upsample_ratio, fy=upsample_ratio, interpolation=cv2.INTER_CUBIC)

    return heatmaps, pafs, scale, pad


def run_inference(net, image_provider, height_size, cpu, track, smooth, no_display, json_view = False):
    net = net.eval()
    if not cpu:
        net = net.cuda()

    stride = 8
    upsample_ratio = 4
    num_keypoints = Pose.num_kpts
    previous_poses = []
    delay = 100
    if isinstance(image_provider, ImageReader):
        delay = 0

    for img in image_provider:
        heatmaps, pafs, scale, pad = infer_fast(net, img, height_size, stride, upsample_ratio, cpu)

        total_keypoints_num = 0
        all_keypoints_by_type = []
        for kpt_idx in range(num_keypoints): 
            total_keypoints_num += extract_keypoints(heatmaps[:, :, kpt_idx], all_keypoints_by_type, total_keypoints_num)

        pose_entries, all_keypoints = group_keypoints(all_keypoints_by_type, pafs, demo=True)
        for kpt_id in range(all_keypoints.shape[0]):
            all_keypoints[kpt_id, 0] = (all_keypoints[kpt_id, 0] * stride / upsample_ratio - pad[1]) / scale
            all_keypoints[kpt_id, 1] = (all_keypoints[kpt_id, 1] * stride / upsample_ratio - pad[0]) / scale
        current_poses = []
        for n, pose_entry in enumerate(pose_entries):
            if len(pose_entry) == 0:
                continue
            pose_keypoints = np.ones((num_keypoints, 2), dtype=np.int32) * -1
            for kpt_id in range(num_keypoints):
                if pose_entry[kpt_id] != -1.0:
                    pose_keypoints[kpt_id, 0] = int(all_keypoints[int(pose_entry[kpt_id]), 0])
                    pose_keypoints[kpt_id, 1] = int(all_keypoints[int(pose_entry[kpt_id]), 1])
            pose = Pose(pose_keypoints, pose_entry[18])
            current_poses.append(pose)

        if json_view == True:
            return current_poses

        if not no_display:
            if track:
                track_poses(previous_poses, current_poses, smooth=smooth)
                previous_poses = current_poses
            for pose in current_poses:
                pose.draw(img)
                
            for pose in current_poses:
                cv2.rectangle(img, (pose.bbox[0], pose.bbox[1]),
                              (pose.bbox[0] + pose.bbox[2], pose.bbox[1] + pose.bbox[3]), (32, 202, 252))
                if track:
                    cv2.putText(img, 'id: {}'.format(pose.id), (pose.bbox[0], pose.bbox[1] - 16),
                                cv2.FONT_HERSHEY_COMPLEX, 0.5, (0, 0, 255))
            cv2.imshow('PoseCamera', img)
            key = cv2.waitKey(delay)
            if key == 27:
                return


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--checkpoint-path', type=str, default='./checkpoint_iter_50000.pth', help='path to the checkpoint')
    parser.add_argument('--height-size', type=int, default=256, help='network input layer height size')
    parser.add_argument('--video', type=str, default='', help='path to video file or camera id')
    parser.add_argument('--images', nargs='+', default='', help='path to input image(s)')
    parser.add_argument('--cpu', action='store_true', help='run network inference on cpu')
    parser.add_argument('--track', type=int, default=0, help='track pose id in video')
    parser.add_argument('--smooth', type=int, default=1, help='smooth pose keypoints')
    parser.add_argument('--no-display', action='store_true', help='hide gui')
    parser.add_argument('--http-server', action='store_true', help='starts http server')
    parser.add_argument('--port', type=int, default=8080, help='http server port')

    args = parser.parse_args()

    if args.video == '' and args.images == '' and  not args.http_server:
        raise ValueError('--video, --image or --http-server has to be provided ')

    net = PoseEstimationWithMobileNet()
    checkpoint = torch.load(args.checkpoint_path, map_location='cpu')
    load_state(net, checkpoint)

    if args.http_server:
        app.run(port = args.port, debug = True)
    else:
        frame_provider = ImageReader(args.images)
        if args.video != '':
            frame_provider = VideoReader(args.video)
        else:
            args.track = 0

        run_inference(net, frame_provider, args.height_size, args.cpu, args.track, args.smooth, args.no_display)
