"""
 * Copyright (c) 2023 Salesforce, Inc.
 * All rights reserved.
 * SPDX-License-Identifier: Apache License 2.0
 * For full license text, see LICENSE.txt file in the repo root or http://www.apache.org/licenses/
 * By Shu Zhang
 * Modified from InstructPix2Pix repo: https://github.com/timothybrooks/instruct-pix2pix
 * Copyright (c) 2023 Timothy Brooks, Aleksander Holynski, Alexei A. Efros.  All rights reserved.
"""

from __future__ import annotations

import math
import random
import os
import sys
import contextlib
from argparse import ArgumentParser

import einops
import k_diffusion as K
import numpy as np
import pandas as pd
import torch
import torch.nn as nn
from einops import rearrange
from omegaconf import OmegaConf
from PIL import Image, ImageOps
from torch import autocast
from tqdm import tqdm

sys.path.append("./stable_diffusion")

from stable_diffusion.ldm.util import instantiate_from_config


class CFGDenoiser(nn.Module):
    def __init__(self, model):
        super().__init__()
        self.inner_model = model

    def forward(self, z, sigma, cond, uncond, text_cfg_scale, image_cfg_scale):
        cfg_z = einops.repeat(z, "1 ... -> n ...", n=3)
        cfg_sigma = einops.repeat(sigma, "1 ... -> n ...", n=3)
        cfg_cond = {
            "c_crossattn": [
                torch.cat(
                    [
                        cond["c_crossattn"][0],
                        uncond["c_crossattn"][0],
                        uncond["c_crossattn"][0],
                    ]
                )
            ],
            "c_concat": [
                torch.cat(
                    [cond["c_concat"][0], cond["c_concat"][0], uncond["c_concat"][0]]
                )
            ],
        }
        out_cond, out_img_cond, out_uncond = self.inner_model(
            cfg_z, cfg_sigma, cond=cfg_cond
        ).chunk(3)
        return (
            out_uncond
            + text_cfg_scale * (out_cond - out_img_cond)
            + image_cfg_scale * (out_img_cond - out_uncond)
        )


def load_model_from_config(config, ckpt, vae_ckpt=None, verbose=False):
    print(f"Loading model from {ckpt}")
    pl_sd = torch.load(ckpt, map_location="cpu")
    if "global_step" in pl_sd:
        print(f"Global Step: {pl_sd['global_step']}")
    sd = pl_sd["state_dict"]
    if vae_ckpt is not None:
        print(f"Loading VAE from {vae_ckpt}")
        vae_sd = torch.load(vae_ckpt, map_location="cpu")["state_dict"]
        sd = {
            k: (
                vae_sd[k[len("first_stage_model.") :]]
                if k.startswith("first_stage_model.")
                else v
            )
            for k, v in sd.items()
        }
    model = instantiate_from_config(config.model)
    m, u = model.load_state_dict(sd, strict=False)
    if len(m) > 0 and verbose:
        print("missing keys:")
        print(m)
    if len(u) > 0 and verbose:
        print("unexpected keys:")
        print(u)
    return model


def get_data(data_path: str) -> pd.DataFrame:
    # Expected columns: ["bg_image_path", "object_to_add", "filename"]
    data = pd.read_csv(data_path)
    return data


def preprocess_images(images_paths: list[str], resolution: int):
    return [
        Image.open(img_path).convert("RGB").resize((resolution, resolution))
        for img_path in images_paths
    ]


def get_images_ids(data):
    return [
        os.path.basename(img_path).split(".")[0]
        for img_path in data["bg_image_path"].to_list()
    ]


def get_args(args):
    parser = ArgumentParser()
    parser.add_argument("--data_path", required=True, type=str)
    parser.add_argument("--output_dir", required=True, type=str)
    parser.add_argument("--resolution", default=512, type=int)
    parser.add_argument("--steps", default=100, type=int)
    parser.add_argument("--config", default="configs/generate.yaml", type=str)
    parser.add_argument("--ckpt", default="checkpoints/hive_rw_label.ckpt", type=str)
    parser.add_argument("--vae-ckpt", default=None, type=str)
    parser.add_argument("--cfg-text", default=7.5, type=float)
    parser.add_argument("--cfg-image", default=1.5, type=float)
    parser.add_argument("--seed", type=int)
    return parser.parse_args(args)


def main():
    args = get_args(sys.argv[1:])
    config = OmegaConf.load(args.config)
    model = load_model_from_config(config, args.ckpt, args.vae_ckpt)
    model.eval().cuda()
    model_wrap = K.external.CompVisDenoiser(model)
    model_wrap_cfg = CFGDenoiser(model_wrap)
    null_token = model.get_learned_conditioning([""])
    seed = random.randint(0, 100000) if args.seed is None else args.seed
    data = get_data(args.data_path)
    bg_images = preprocess_images(data["bg_image_path"].to_list(), args.resolution)
    with torch.no_grad(), autocast(
        "cuda"
    ), model.ema_scope(), contextlib.redirect_stdout(None):
        cond = {}
        for bg_image, object_to_add, filename in tqdm(
            zip(
                bg_images,
                data["object_to_add"].to_list(),
                data["filename"].to_list(),
            )
        ):
            edit = f"add a {object_to_add}, image quality is five out of five"
            cond["c_crossattn"] = [model.get_learned_conditioning([edit])]
            bg_image = 2 * torch.tensor(np.array(bg_image)).float() / 255 - 1
            bg_image = rearrange(bg_image, "h w c -> 1 c h w").to(model.device)
            cond["c_concat"] = [model.encode_first_stage(bg_image).mode()]

            uncond = {}
            uncond["c_crossattn"] = [null_token]
            uncond["c_concat"] = [torch.zeros_like(cond["c_concat"][0])]

            sigmas = model_wrap.get_sigmas(args.steps)

            extra_args = {
                "cond": cond,
                "uncond": uncond,
                "text_cfg_scale": args.cfg_text,
                "image_cfg_scale": args.cfg_image,
            }
            torch.manual_seed(seed)
            z = torch.randn_like(cond["c_concat"][0]) * sigmas[0]
            z = K.sampling.sample_euler_ancestral(
                model_wrap_cfg, z, sigmas, extra_args=extra_args
            )
            x = model.decode_first_stage(z)
            x = torch.clamp((x + 1.0) / 2.0, min=0.0, max=1.0)
            x = 255.0 * rearrange(x, "1 c h w -> h w c")
            edited_image = Image.fromarray(x.type(torch.uint8).cpu().numpy())
            edited_image.save(os.path.join(args.output_dir, f"{filename}.png"))


if __name__ == "__main__":
    main()
