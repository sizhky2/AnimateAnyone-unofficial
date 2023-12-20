import argparse
import datetime
import inspect
import os
import random
import numpy as np

from PIL import Image
from omegaconf import OmegaConf
from collections import OrderedDict

import torch
import torch.distributed as dist

from diffusers import AutoencoderKL, DDIMScheduler, UniPCMultistepScheduler

from tqdm import tqdm
from transformers import CLIPTextModel, CLIPTokenizer, CLIPProcessor

from models.PoseGuider import PoseGuider
from models.ReferenceEncoder import ReferenceEncoder
from models.ReferenceNet import ReferenceNet
from models.ReferenceNet_attention import ReferenceNetAttention
from models.unet import UNet3DConditionModel
from pipelines.pipeline_stage_1 import AnimationAnyonePipeline
from diffusers.models import UNet2DConditionModel


from utils.util import save_videos_grid
from utils.dist_tools import distributed_init
from utils.videoreader import VideoReader

from accelerate.utils import set_seed
from einops import rearrange
from pathlib import Path

from decord import VideoReader as decord_VideoReader
import cv2
import pdb
from fastcore.basics import patch, patch_to

@patch_to(ReferenceNet, cls_method=True)
def load_referencenet(cls, pretrained_model_path):
    print(f"loaded ReferenceNet's pretrained weights from {pretrained_model_path} ...")

    config = {
        "_class_name": "UNet2DConditionModel",
        "_diffusers_version": "0.6.0",
        "act_fn": "silu",
        "attention_head_dim": 8,
        "block_out_channels": [320, 640, 1280, 1280],
        "center_input_sample": False,
        "cross_attention_dim": 768,
        "down_block_types": [
            "CrossAttnDownBlock2D",
            "CrossAttnDownBlock2D",
            "CrossAttnDownBlock2D",
            "DownBlock2D"
        ],
        "downsample_padding": 1,
        "flip_sin_to_cos": True,
        "freq_shift": 0,
        "in_channels": 4,
        "layers_per_block": 2,
        "mid_block_scale_factor": 1,
        "norm_eps": 1e-05,
        "norm_num_groups": 32,
        "out_channels": 4,
        "sample_size": 64,
        "up_block_types": [
            "UpBlock2D",
            "CrossAttnUpBlock2D",
            "CrossAttnUpBlock2D",
            "CrossAttnUpBlock2D"
        ]
    }

    # from diffusers.utils import WEIGHTS_NAME
    model = cls.from_config(config)
    # model_file = os.path.join(pretrained_model_path, WEIGHTS_NAME)
    model_file = pretrained_model_path
    
    if not os.path.isfile(model_file):
        raise RuntimeError(f"{model_file} does not exist")
    state_dict = torch.load(model_file, map_location="cpu")

    m, u = model.load_state_dict(state_dict['referencenet_state_dict'], strict=True)
    if m or u:
        print(f"### missing keys: {len(m)}; \n### unexpected keys: {len(u)};")
        print(f"### missing keys:\n{m}\n### unexpected keys:\n{u}\n")
    
    # params = [p.numel() for n, p in model.named_parameters() if "2D" in n]
    # print(f"### 2D Module Parameters: {sum(params) / 1e6} M")
    
    params = [p.numel() for n, p in model.named_parameters()]
    print(f"### Module Parameters: {sum(params) / 1e6} M")
    return model


def load_models(args):
    *_, func_args = inspect.getargvalues(inspect.currentframe())
    func_args = dict(func_args)
    
    config  = OmegaConf.load(args.config)
      
    # Initialize distributed training
    device = torch.device(f"cuda:{args.rank}")
    dist_kwargs = {"rank":args.rank, "world_size":args.world_size, "dist":args.dist}
    
    if 'savename' not in config or config.savename is None:
        time_str = datetime.datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
        savedir = f"samples/{Path(args.config).stem}-{time_str}"
    else:
        savedir = f"samples/{config.savename}"
        
    if args.dist:
        dist.broadcast_object_list([savedir], 0)
        dist.barrier()
    
    if args.rank == 0:
        os.makedirs(savedir, exist_ok=True)
        
    ### >>> create animation pipeline >>> ###
    tokenizer = CLIPTokenizer.from_pretrained(config.pretrained_model_path, subfolder="tokenizer")
    # tokenizer = CLIPTokenizer.from_pretrained(config.pretrained_clip_path)
    
    text_encoder = CLIPTextModel.from_pretrained(config.pretrained_model_path, subfolder="text_encoder")
    # text_encoder = CLIPTextModel.from_pretrained(config.pretrained_clip_path)
    
    unet = UNet2DConditionModel.from_pretrained(config.pretrained_model_path, subfolder="unet")
    vae = AutoencoderKL.from_pretrained(config.pretrained_model_path, subfolder="vae")
    
    print(config.pretrained_poseguider_path) # /mnt/f/research/HumanVideo/AnimateAnyone-unofficial/poseguider_test_v0.ckpt
    poseguider = PoseGuider.from_pretrained(pretrained_model_path=config.pretrained_poseguider_path)
    clip_image_encoder = ReferenceEncoder(model_path=config.clip_model_path)
    clip_image_processor = CLIPProcessor.from_pretrained(config.clip_model_path,local_files_only=True)
    
    referencenet = ReferenceNet.load_referencenet(pretrained_model_path=config.pretrained_referencenet_path)
    
    # reference_control_writer = ReferenceNetAttention(referencenet, do_classifier_free_guidance=True, mode='write', fusion_blocks=config.fusion_blocks)
    # reference_control_reader = ReferenceNetAttention(unet, do_classifier_free_guidance=True, mode='read', fusion_blocks=config.fusion_blocks)
    reference_control_writer = None
    reference_control_reader = None
    

    # unet.enable_xformers_memory_efficient_attention()
    # referencenet.enable_xformers_memory_efficient_attention()

    vae.to(torch.float32)
    unet.to(torch.float32)
    text_encoder.to(torch.float32)
    referencenet.to(torch.float32).to(device)
    poseguider.to(torch.float32).to(device)
    clip_image_encoder.to(torch.float32).to(device)
    
    pipeline = AnimationAnyonePipeline(
        vae=vae, text_encoder=text_encoder, tokenizer=tokenizer, unet=unet,
        scheduler=DDIMScheduler(**OmegaConf.to_container(config.noise_scheduler_kwargs)),
        # NOTE: UniPCMultistepScheduler
    )
    return pipeline, config, device, reference_control_writer, reference_control_reader, referencenet, poseguider, clip_image_processor, clip_image_encoder, dist_kwargs, savedir

def main(args):
    from torch_snippets import dumpdill
    dumpdill(args, '/tmp/tmp.args')
    pipeline, config, device, reference_control_writer, reference_control_reader, referencenet, poseguider, clip_image_processor, clip_image_encoder, dist_kwargs, savedir = load_models(args)
    pipeline.to(device)
    
    # exit(0)
    
    ### <<< create validation pipeline <<< ###
    
    random_seeds = config.get("seed", [-1])
    random_seeds = [random_seeds] if isinstance(random_seeds, int) else list(random_seeds)
    random_seeds = random_seeds * len(config.source_image) if len(random_seeds) == 1 else random_seeds
    
    # input test videos (either source video/ conditions)
    
    test_videos = config.video_path
    source_images = config.source_image
    num_actual_inference_steps = config.get("num_actual_inference_steps", config.steps)

    # read size, step from yaml file
    sizes = [config.size] * len(test_videos)
    steps = [config.S] * len(test_videos)

    config.random_seed = []
    prompt = n_prompt = "none"
    for idx, (source_image, test_video, random_seed, size, step) in tqdm(
        enumerate(zip(source_images, test_videos, random_seeds, sizes, steps)), 
        total=len(test_videos), 
        disable=(args.rank!=0)
    ):
        samples_per_video = []
        samples_per_clip = []
        # manually set random seed for reproduction
        if random_seed != -1: 
            torch.manual_seed(random_seed)
            set_seed(random_seed)
        else:
            torch.seed()
        config.random_seed.append(torch.initial_seed())

        if test_video.endswith('.mp4'):
            control = VideoReader(test_video).read()
            if control[0].shape[0] != size:
                control = [np.array(Image.fromarray(c).resize((size, size))) for c in control]
            if config.max_length is not None:
                control = control[config.offset: (config.offset+config.max_length)]
            control = np.array(control)
        
        if source_image.endswith(".mp4"):
            source_image = np.array(Image.fromarray(VideoReader(source_image).read()[0]).resize((size, size)))
        else:
            source_image = np.array(Image.open(source_image).resize((size, size)))
        H, W, C = source_image.shape
        
        
        print(f"current seed: {torch.initial_seed()}")
        init_latents = None
        
        # print(f"sampling {prompt} ...")
        # 满足16的整数倍
        original_length = control.shape[0]
        if control.shape[0] % config.L > 0:
            control = np.pad(control, ((0, config.L-control.shape[0] % config.L), (0, 0), (0, 0), (0, 0)), mode='edge')
        
        idx_control = random.randint(0,control.shape[0]-1)
        control = control[idx_control] # (256, 256, 3)
        
        generator = torch.Generator(device=torch.device("cuda:0"))
        generator.manual_seed(torch.initial_seed())
        sample = pipeline(
            prompt,
            negative_prompt         = n_prompt,
            num_inference_steps     = config.steps,
            guidance_scale          = config.guidance_scale,
            width                   = W,
            height                  = H,
            video_length            = len(control),
            init_latents            = init_latents,
            generator               = generator,
            num_actual_inference_steps = num_actual_inference_steps,
            reference_control_writer = reference_control_writer,
            reference_control_reader = reference_control_reader,
            source_image             = source_image,
            referencenet             = referencenet,
            poseguider               = poseguider,
            clip_image_processor     = clip_image_processor,
            clip_image_encoder       = clip_image_encoder,
            pose_condition           = control,
            **dist_kwargs,
        ).videos

        
        # print(sample.shape) # torch.Size([1, 256, 256, 3])
        
        
        modify_original_length = 1
        
        if args.rank == 0:
            source_images = np.array([source_image] * modify_original_length)
            source_images = rearrange(torch.from_numpy(source_images), "t h w c -> 1 c t h w") / 255.0
            samples_per_video.append(source_images)
            
            control = control / 255.0
            # control = rearrange(control, "t h w c -> 1 c t h w")
            control = rearrange(control, "h w c -> 1 c 1 h w")
            control = torch.from_numpy(control)
            
            # add
            sample = rearrange(sample,"1 h w c -> 1 c 1 h w")
            # pdb.set_trace()
            
            samples_per_video.append(control[:, :, :modify_original_length])
            samples_per_video.append(sample[:, :, :modify_original_length])
            
            # print(samples_per_video.size())
            
            samples_per_video = torch.cat(samples_per_video)

            video_name = os.path.basename(test_video)[:-4]
            source_name = os.path.basename(config.source_image[idx]).split(".")[0]
            # save_videos_grid(samples_per_video[-1:], f"{savedir}/videos/{source_name}_{video_name}.mp4")
            save_videos_grid(samples_per_video, f"{savedir}/videos/{source_name}_{video_name}/grid.mp4",fps=1)
            
            vr = decord_VideoReader(f"{savedir}/videos/{source_name}_{video_name}/grid.mp4")
            frame = vr[0].asnumpy()
            cv2.imwrite(f"{savedir}/videos/{source_name}_{video_name}/grid.png", cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))

            if config.save_individual_videos:
                save_videos_grid(samples_per_video[1:2], f"{savedir}/videos/{source_name}_{video_name}/ctrl.mp4")
                save_videos_grid(samples_per_video[0:1], f"{savedir}/videos/{source_name}_{video_name}/orig.mp4")
                
        if args.dist:
            dist.barrier()
               
    if args.rank == 0:
        OmegaConf.save(config, f"{savedir}/config.yaml")


def distributed_main(device_id, args):
    args.rank = device_id
    args.device_id = device_id
    if torch.cuda.is_available():
        torch.cuda.set_device(args.device_id)
        torch.cuda.init()
    distributed_init(args)
    main(args)


def run(args):

    if args.dist:
        args.world_size = max(1, torch.cuda.device_count())
        assert args.world_size <= torch.cuda.device_count()

        if args.world_size > 0 and torch.cuda.device_count() > 1:
            port = random.randint(10000, 20000)
            args.init_method = f"tcp://localhost:{port}"
            torch.multiprocessing.spawn(
                fn=distributed_main,
                args=(args,),
                nprocs=args.world_size,
            )
    else:
        main(args)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=str, required=True)
    parser.add_argument("--dist", action="store_true", required=False)
    parser.add_argument("--rank", type=int, default=0, required=False)
    parser.add_argument("--world_size", type=int, default=1, required=False)

    args = parser.parse_args()
    run(args)
    
    # python3 -m pipelines.animation_stage_1 --config configs/prompts/animation_stage_1.yaml