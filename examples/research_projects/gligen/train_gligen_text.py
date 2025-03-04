# from accelerate.utils import write_basic_config
#
# write_basic_config()
import argparse
import logging
import math
import os
import shutil
from pathlib import Path

import accelerate
import torch
import torch.nn.functional as F
import torch.utils.checkpoint
import transformers
from accelerate import Accelerator
from accelerate.logging import get_logger
from accelerate.utils import ProjectConfiguration, set_seed
from packaging import version
from tqdm.auto import tqdm

import diffusers
from diffusers import (
    AutoencoderKL,
    DDPMScheduler,
    EulerDiscreteScheduler,
    StableDiffusionGLIGENPipeline,
    UNet2DConditionModel,
)
from diffusers.optimization import get_scheduler
from diffusers.utils import is_wandb_available, make_image_grid
from diffusers.utils.import_utils import is_xformers_available
from diffusers.utils.torch_utils import is_compiled_module


if is_wandb_available():
    pass

# Will error if the minimal version of diffusers is not installed. Remove at your own risks.
# check_min_version("0.28.0.dev0")

logger = get_logger(__name__)

from common_api_schnauby.camera_vision_utils import load_txt_to_dto as load_txt_to_dto
from common_api_schnauby.dtos import *
from datasets_schnauby.datasets import load_bounding_boxes
from PIL import Image
import wandb

@torch.no_grad()
def log_validation(vae, text_encoder, tokenizer, unet, noise_scheduler, args, accelerator, step, weight_dtype, val_folder_path):
    print("Running validation")
    
    
    print("setup the model")
    
    unet = accelerator.unwrap_model(unet)
    vae.to(accelerator.device, dtype=torch.float32)

    pipeline = StableDiffusionGLIGENPipeline(
        vae,
        text_encoder,
        tokenizer,
        unet,
        EulerDiscreteScheduler.from_config(noise_scheduler.config),
        safety_checker=None,
        feature_extractor=None,
    )
    pipeline = pipeline.to(accelerator.device)
    pipeline.set_progress_bar_config(disable=not accelerator.is_main_process)
    if args.enable_xformers_memory_efficient_attention:
        pipeline.enable_xformers_memory_efficient_attention()

    if args.seed is None:
        generator = None
    else:
        generator = torch.Generator(device=accelerator.device).manual_seed(args.seed)
    
    
    samples = [f for f in os.listdir(val_folder_path) if f.endswith('.txt')]
    if len(samples) == 0:
        print(f'No samples found in {val_folder_path}')
        return
    print(f'Found {len(samples)} samples in {val_folder_path}')
    
    
    formatted_images = [] #for wandb
    
    for sample in samples:
        scene = load_txt_to_dto(os.path.join(val_folder_path, sample))    
        bb_file = sample[:-4] + '.bb'
        bounding_boxes = load_bounding_boxes(os.path.join(val_folder_path, bb_file))
        image_file = sample[:-4] + '.png'
        captions = []  
        for object in scene.objects:
            rotation = object.rot
            if rotation is None:
                rotation = Rotation(0, 0, 0)
            rotation_angles = (rotation.x, rotation.y, rotation.z)
            caption = f"A {object.form.size.name} {object.form.color.name} {object.form.type.name} with rotation {rotation_angles}"     
            # copied from make_datasets.py
            captions.append(caption)

        boxes = bounding_boxes
        gligen_phrases = captions
        prompt = scene.prompt
        
        inf_steps = 50
        if os.name == 'nt' or platform.system() == 'Windows':
            inf_steps = 1
        
        images = None 
        if len(bounding_boxes) == 0:
            gligen_phrases = ["empty"]
            boxes = [[0.0, 0.0, 1.0, 1.0]]
       
        print("Generating images")
        print(gligen_phrases, bounding_boxes)
        print(f"length is ok: {len(gligen_phrases) == len(boxes)}")
        
        images = pipeline(
            prompt=prompt,
            gligen_phrases=gligen_phrases,
            gligen_boxes=boxes,
            gligen_scheduled_sampling_beta=1.0,
            output_type="pil",
            num_inference_steps=inf_steps,
            negative_prompt="",
            num_images_per_prompt=4,
            generator=generator,
        ).images
        
        
        skipSave = False
        
        for tracker in accelerator.trackers:
            if tracker.name == "wandb":
                skipSave = True
                formatted_images.append(wandb.Image(Image.open(os.path.join(val_folder_path, image_file)), caption=f"ground trouth: {image_file}"))
                for image in images:
                        image = wandb.Image(image, caption=f"{prompt}: {image_file}")
                        formatted_images.append(image)
            

        if not skipSave:
        # Save the generated images or upload to wandd
            os.makedirs(os.path.join(args.output_dir, "images"), exist_ok=True)
            make_image_grid(images, 1, 4).save(
                os.path.join(args.output_dir, "images", f"generated-images-{step:06d}-{accelerator.process_index:02d}.png")
            )
    for tracker in accelerator.trackers:
        if tracker.name == "wandb":
            tracker.log({"validation": formatted_images})
    #last line idk what it does
    vae.to(accelerator.device, dtype=weight_dtype)
    print("Validation done")

def parse_args(input_args=None):
    parser = argparse.ArgumentParser(description="Simple example of a ControlNet training script.")
    parser.add_argument(
        "--data_path_training",
        type=str,
        required=True,
        help="Path to training dataset folder.",
    )
    parser.add_argument(
        "--data_path_validation",
        type=str,
        required=True,
        help="Path to validation dataset folder.",
    )
    parser.add_argument(
        "--output_dir",
        type=str,
        required=True,
        help="The output directory where the model predictions and checkpoints will be written.",
    )
    parser.add_argument("--seed", type=int, default=0, help="A seed for reproducible training.")
    parser.add_argument(
        "--resolution",
        type=int,
        default=512,
        help=(
            "The resolution for input images, all the images in the train/validation dataset will be resized to this"
            " resolution"
        ),
    )
    parser.add_argument(
        "--train_batch_size", type=int, default=4, help="Batch size (per device) for the training dataloader."
    )
    parser.add_argument("--num_train_epochs", type=int, default=1)
    parser.add_argument(
        "--max_train_steps",
        type=int,
        default=None,
        help="Total number of training steps to perform.  If provided, overrides num_train_epochs.",
    )
    parser.add_argument(
        "--checkpointing_steps",
        type=int,
        default=500,
        help=(
            "Save a checkpoint of the training state every X updates. Checkpoints can be used for resuming training via `--resume_from_checkpoint`. "
            "In the case that the checkpoint is better than the final trained model, the checkpoint can also be used for inference."
            "Using a checkpoint for inference requires separate loading of the original pipeline and the individual checkpointed model components."
            "See https://huggingface.co/docs/diffusers/main/en/training/dreambooth#performing-inference-using-a-saved-checkpoint for step by step"
            "instructions."
        ),
    )
    parser.add_argument(
        "--checkpoints_total_limit",
        type=int,
        default=None,
        help=("Max number of checkpoints to store."),
    )
    parser.add_argument(
        "--resume_from_checkpoint",
        type=str,
        default=None,
        help=(
            "Whether training should be resumed from a previous checkpoint. Use a path saved by"
            ' `--checkpointing_steps`, or `"latest"` to automatically select the last available checkpoint.'
        ),
    )
    parser.add_argument(
        "--gradient_accumulation_steps",
        type=int,
        default=1,
        help="Number of updates steps to accumulate before performing a backward/update pass.",
    )
    parser.add_argument(
        "--gradient_checkpointing",
        action="store_true",
        help="Whether or not to use gradient checkpointing to save memory at the expense of slower backward pass.",
    )
    parser.add_argument(
        "--learning_rate",
        type=float,
        default=5e-6,
        help="Initial learning rate (after the potential warmup period) to use.",
    )
    parser.add_argument(
        "--scale_lr",
        action="store_true",
        default=False,
        help="Scale the learning rate by the number of GPUs, gradient accumulation steps, and batch size.",
    )
    parser.add_argument(
        "--lr_scheduler",
        type=str,
        default="constant",
        help=(
            'The scheduler type to use. Choose between ["linear", "cosine", "cosine_with_restarts", "polynomial",'
            ' "constant", "constant_with_warmup"]'
        ),
    )
    parser.add_argument(
        "--lr_warmup_steps", type=int, default=500, help="Number of steps for the warmup in the lr scheduler."
    )
    parser.add_argument(
        "--lr_num_cycles",
        type=int,
        default=1,
        help="Number of hard resets of the lr in cosine_with_restarts scheduler.",
    )
    parser.add_argument("--lr_power", type=float, default=1.0, help="Power factor of the polynomial scheduler.")
    parser.add_argument(
        "--dataloader_num_workers",
        type=int,
        default=0,
        help=(
            "Number of subprocesses to use for data loading. 0 means that the data will be loaded in the main process."
        ),
    )
    parser.add_argument("--adam_beta1", type=float, default=0.9, help="The beta1 parameter for the Adam optimizer.")
    parser.add_argument("--adam_beta2", type=float, default=0.999, help="The beta2 parameter for the Adam optimizer.")
    parser.add_argument("--adam_weight_decay", type=float, default=1e-2, help="Weight decay to use.")
    parser.add_argument("--adam_epsilon", type=float, default=1e-08, help="Epsilon value for the Adam optimizer")
    parser.add_argument("--max_grad_norm", default=1.0, type=float, help="Max gradient norm.")
    parser.add_argument(
        "--logging_dir",
        type=str,
        default="logs",
        help=(
            "[TensorBoard](https://www.tensorflow.org/tensorboard) log directory. Will default to"
            " *output_dir/runs/**CURRENT_DATETIME_HOSTNAME***."
        ),
    )
    parser.add_argument(
        "--allow_tf32",
        action="store_true",
        help=(
            "Whether or not to allow TF32 on Ampere GPUs. Can be used to speed up training. For more information, see"
            " https://pytorch.org/docs/stable/notes/cuda.html#tensorfloat-32-tf32-on-ampere-devices"
        ),
    )
    parser.add_argument(
        "--report_to",
        type=str,
        default="tensorboard",
        help=(
            'The integration to report the results and logs to. Supported platforms are `"tensorboard"`'
            ' (default), `"wandb"` and `"comet_ml"`. Use `"all"` to report to all integrations.'
        ),
    )
    parser.add_argument(
        "--mixed_precision",
        type=str,
        default=None,
        choices=["no", "fp16", "bf16"],
        help=(
            "Whether to use mixed precision. Choose between fp16 and bf16 (bfloat16). Bf16 requires PyTorch >="
            " 1.10.and an Nvidia Ampere GPU.  Default to the value of accelerate config of the current system or the"
            " flag passed with the `accelerate.launch` command. Use this argument to override the accelerate config."
        ),
    )
    parser.add_argument(
        "--enable_xformers_memory_efficient_attention", action="store_true", help="Whether or not to use xformers."
    )
    parser.add_argument(
        "--set_grads_to_none",
        action="store_true",
        help=(
            "Save more memory by using setting grads to None instead of zero. Be aware, that this changes certain"
            " behaviors, so disable this argument if it causes any problems. More info:"
            " https://pytorch.org/docs/stable/generated/torch.optim.Optimizer.zero_grad.html"
        ),
    )
    parser.add_argument(
        "--tracker_project_name",
        type=str,
        default="train_controlnet",
        help=(
            "The `project_name` argument passed to Accelerator.init_trackers for"
            " more information see https://huggingface.co/docs/accelerate/v0.17.0/en/package_reference/accelerator#accelerate.Accelerator"
        ),
    )
    args = parser.parse_args()
    print("lets go args are parsed")
    return args


def main(args):
    logging_dir = Path(args.output_dir, args.logging_dir)

    accelerator_project_config = ProjectConfiguration(project_dir=args.output_dir, logging_dir=logging_dir)

    accelerator = Accelerator(
        gradient_accumulation_steps=args.gradient_accumulation_steps,
        mixed_precision=args.mixed_precision,
        log_with=args.report_to,
        project_config=accelerator_project_config,
    )

    # Disable AMP for MPS.
    if torch.backends.mps.is_available():
        accelerator.native_amp = False

    # Make one log on every process with the configuration for debugging.
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        level=logging.INFO,
    )
    logger.info(accelerator.state, main_process_only=False)
    if accelerator.is_local_main_process:
        transformers.utils.logging.set_verbosity_warning()
        diffusers.utils.logging.set_verbosity_info()
    else:
        transformers.utils.logging.set_verbosity_error()
        diffusers.utils.logging.set_verbosity_error()

    # If passed along, set the training seed now.
    if args.seed is not None:
        set_seed(args.seed)

    # Handle the repository creation
    if accelerator.is_main_process:
        if args.output_dir is not None:
            os.makedirs(args.output_dir, exist_ok=True)

    # import correct text encoder class
    # text_encoder_cls = import_model_class_from_model_name_or_path(args.pretrained_model_name_or_path, args.revision)
    # Load scheduler and models
    from transformers import CLIPTextModel, CLIPTokenizer
    cache_dir = "/tmp/models_cache"
    print("loading pretrained models etc and cache them into: ", cache_dir)
    #pretrained_model_name_or_path = "gligen/diffusers-generation-text-box" #the one from hgf
    pretrained_model_name_or_path = "masterful/gligen-1-4-generation-text-box" #the one original
    #pretrained_unet = "Hzzone/GLIGEN_COCO" #pretrained by the creator of this script
    # maybe i need the unet from Hzzones repo
    print("Loading tokenizer")
    tokenizer = CLIPTokenizer.from_pretrained(pretrained_model_name_or_path, subfolder="tokenizer", cache_dir=cache_dir)
    print("Loading noise scheduler")
    noise_scheduler = DDPMScheduler.from_pretrained(pretrained_model_name_or_path, subfolder="scheduler", cache_dir=cache_dir)
    print("Loading text encoder")
    text_encoder = CLIPTextModel.from_pretrained(pretrained_model_name_or_path, subfolder="text_encoder", cache_dir=cache_dir)
    print("Loading vae")
    vae = AutoencoderKL.from_pretrained(pretrained_model_name_or_path, subfolder="vae",cache_dir=cache_dir)
    print("Loading unet")
    unet = UNet2DConditionModel.from_pretrained(pretrained_model_name_or_path, subfolder="unet",cache_dir=cache_dir)

    # Taken from [Sayak Paul's Diffusers PR #6511](https://github.com/huggingface/diffusers/pull/6511/files)
    def unwrap_model(model):
        model = accelerator.unwrap_model(model)
        model = model._orig_mod if is_compiled_module(model) else model
        return model

    # `accelerate` 0.16.0 will have better support for customized saving
    if version.parse(accelerate.__version__) >= version.parse("0.16.0"):
        # create custom saving & loading hooks so that `accelerator.save_state(...)` serializes in a nice format
        def save_model_hook(models, weights, output_dir):
            if accelerator.is_main_process:
                i = len(weights) - 1

                while len(weights) > 0:
                    weights.pop()
                    model = models[i]

                    sub_dir = "unet"
                    model.save_pretrained(os.path.join(output_dir, sub_dir))

                    i -= 1

        def load_model_hook(models, input_dir):
            while len(models) > 0:
                # pop models so that they are not loaded again
                model = models.pop()

                # load diffusers style into model
                load_model = unet.from_pretrained(input_dir, subfolder="unet")
                model.register_to_config(**load_model.config)

                model.load_state_dict(load_model.state_dict())
                del load_model

        accelerator.register_save_state_pre_hook(save_model_hook)
        accelerator.register_load_state_pre_hook(load_model_hook)

    vae.requires_grad_(False)
    unet.requires_grad_(False)
    text_encoder.requires_grad_(False)

    if args.enable_xformers_memory_efficient_attention:
        if is_xformers_available():
            import xformers

            xformers_version = version.parse(xformers.__version__)
            if xformers_version == version.parse("0.0.16"):
                logger.warning(
                    "xFormers 0.0.16 cannot be used for training in some GPUs. If you observe problems during training, please update xFormers to at least 0.0.17. See https://huggingface.co/docs/diffusers/main/en/optimization/xformers for more details."
                )
            unet.enable_xformers_memory_efficient_attention()
            # controlnet.enable_xformers_memory_efficient_attention()
        else:
            raise ValueError("xformers is not available. Make sure it is installed correctly")

    # if args.gradient_checkpointing:
    #     controlnet.enable_gradient_checkpointing()

    # Check that all trainable models are in full precision
    low_precision_error_string = (
        " Please make sure to always have all model weights in full float32 precision when starting training - even if"
        " doing mixed precision training, copy of the weights should still be float32."
    )

    if unwrap_model(unet).dtype != torch.float32:
        raise ValueError(f"Controlnet loaded as datatype {unwrap_model(unet).dtype}. {low_precision_error_string}")

    # Enable TF32 for faster training on Ampere GPUs,
    # cf https://pytorch.org/docs/stable/notes/cuda.html#tensorfloat-32-tf32-on-ampere-devices
    if args.allow_tf32:
        torch.backends.cuda.matmul.allow_tf32 = True

    if args.scale_lr:
        args.learning_rate = (
            args.learning_rate * args.gradient_accumulation_steps * args.train_batch_size * accelerator.num_processes
        )

    print("setup: optimizer class")
    optimizer_class = torch.optim.AdamW
    # Optimizer creation
    for n, m in unet.named_modules():
        if ("fuser" in n) or ("position_net" in n):
            import torch.nn as nn

            if isinstance(m, (nn.Linear, nn.LayerNorm)):
                m.reset_parameters()
    params_to_optimize = []
    for n, p in unet.named_parameters():
        if ("fuser" in n) or ("position_net" in n):
            p.requires_grad = True
            params_to_optimize.append(p)
    optimizer = optimizer_class(
        params_to_optimize,
        lr=args.learning_rate,
        betas=(args.adam_beta1, args.adam_beta2),
        weight_decay=args.adam_weight_decay,
        eps=args.adam_epsilon,
    )

    from datasets_schnauby.datasets import BoundingBoxDataset
    
    print("Loading clip encoder to cuda")
    encoder=text_encoder.cuda()    
    print("Loading dataset")
    train_dataset = BoundingBoxDataset(args.data_path_training, tokenizer, encoder)

    train_dataloader = torch.utils.data.DataLoader(
        train_dataset,
        shuffle=True,
        # collate_fn=collate_fn,
        batch_size=args.train_batch_size,
        num_workers=args.dataloader_num_workers,
    )

    # Scheduler and math around the number of training steps.
    overrode_max_train_steps = False
    num_update_steps_per_epoch = math.ceil(len(train_dataloader) / args.gradient_accumulation_steps)
    if args.max_train_steps is None:
        args.max_train_steps = args.num_train_epochs * num_update_steps_per_epoch
        overrode_max_train_steps = True

    lr_scheduler = get_scheduler(
        args.lr_scheduler,
        optimizer=optimizer,
        num_warmup_steps=args.lr_warmup_steps * accelerator.num_processes,
        num_training_steps=args.max_train_steps * accelerator.num_processes,
        num_cycles=args.lr_num_cycles,
        power=args.lr_power,
    )

    # Prepare everything with our `accelerator`.
    unet, optimizer, train_dataloader, lr_scheduler = accelerator.prepare(
        unet, optimizer, train_dataloader, lr_scheduler
    )

    # For mixed precision training we cast the text_encoder and vae weights to half-precision
    # as these models are only used for inference, keeping weights in full precision is not required.
    weight_dtype = torch.float32
    if accelerator.mixed_precision == "fp16":
        weight_dtype = torch.float16
    elif accelerator.mixed_precision == "bf16":
        weight_dtype = torch.bfloat16

    # Move vae, unet and text_encoder to device and cast to weight_dtype
    vae.to(accelerator.device, dtype=weight_dtype)
    # unet.to(accelerator.device, dtype=weight_dtype)
    unet.to(accelerator.device, dtype=torch.float32)
    text_encoder.to(accelerator.device, dtype=weight_dtype)

    # We need to recalculate our total training steps as the size of the training dataloader may have changed.
    num_update_steps_per_epoch = math.ceil(len(train_dataloader) / args.gradient_accumulation_steps)
    if overrode_max_train_steps:
        args.max_train_steps = args.num_train_epochs * num_update_steps_per_epoch
    # Afterwards we recalculate our number of training epochs
    args.num_train_epochs = math.ceil(args.max_train_steps / num_update_steps_per_epoch)

    # We need to initialize the trackers we use, and also store our configuration.
    # The trackers initializes automatically on the main process.
    if accelerator.is_main_process:
        tracker_config = dict(vars(args))

        # tensorboard cannot handle list types for config
        # tracker_config.pop("validation_prompt")
        # tracker_config.pop("validation_image")

        accelerator.init_trackers(args.tracker_project_name, config=tracker_config)

    # Train!
    # total_batch_size = args.train_batch_size * accelerator.num_processes * args.gradient_accumulation_steps

    # logger.info("***** Running training *****")
    # logger.info(f"  Num examples = {len(train_dataset)}")
    # logger.info(f"  Num batches each epoch = {len(train_dataloader)}")
    # logger.info(f"  Num Epochs = {args.num_train_epochs}")
    # logger.info(f"  Instantaneous batch size per device = {args.train_batch_size}")
    # logger.info(f"  Total train batch size (w. parallel, distributed & accumulation) = {total_batch_size}")
    # logger.info(f"  Gradient Accumulation steps = {args.gradient_accumulation_steps}")
    # logger.info(f"  Total optimization steps = {args.max_train_steps}")
    global_step = 0
    first_epoch = 0

    # Potentially load in the weights and states from a previous save
    if args.resume_from_checkpoint:
        if args.resume_from_checkpoint != "latest":
            path = os.path.basename(args.resume_from_checkpoint)
        else:
            # Get the most recent checkpoint
            dirs = os.listdir(args.output_dir)
            dirs = [d for d in dirs if d.startswith("checkpoint")]
            dirs = sorted(dirs, key=lambda x: int(x.split("-")[1]))
            path = dirs[-1] if len(dirs) > 0 else None

        if path is None:
            accelerator.print(
                f"Checkpoint '{args.resume_from_checkpoint}' does not exist. Starting a new training run."
            )
            args.resume_from_checkpoint = None
            initial_global_step = 0
        else:
            accelerator.print(f"Resuming from checkpoint {path}")
            accelerator.load_state(os.path.join(args.output_dir, path))
            global_step = int(path.split("-")[1])

            initial_global_step = global_step
            first_epoch = global_step // num_update_steps_per_epoch
    else:
        initial_global_step = 0

    progress_bar = tqdm(
        range(0, args.max_train_steps),
        initial=initial_global_step,
        desc="Steps",
        # Only show the progress bar once on each machine.
        disable=not accelerator.is_local_main_process,
    )

    log_validation(
        vae,
        text_encoder,
        tokenizer,
        unet,
        noise_scheduler,
        args,
        accelerator,
        global_step,
        weight_dtype,
        args.data_path_validation,
        
    )

    # image_logs = None
    for epoch in range(first_epoch, args.num_train_epochs):
        for step, batch in enumerate(train_dataloader):
            with accelerator.accumulate(unet):
                # Convert images to latent space
                latents = vae.encode(batch["pixel_values"].to(dtype=weight_dtype)).latent_dist.sample()
                latents = latents * vae.config.scaling_factor

                # Sample noise that we'll add to the latents
                noise = torch.randn_like(latents)
                bsz = latents.shape[0]
                # Sample a random timestep for each image
                timesteps = torch.randint(0, noise_scheduler.config.num_train_timesteps, (bsz,), device=latents.device)
                timesteps = timesteps.long()

                # Add noise to the latents according to the noise magnitude at each timestep
                # (this is the forward diffusion process)
                noisy_latents = noise_scheduler.add_noise(latents, noise, timesteps)

                with torch.no_grad():
                    # Get the text embedding for conditioning
                    encoder_hidden_states = text_encoder(
                        batch["caption"]["input_ids"].squeeze(1),
                        # batch['caption']['attention_mask'].squeeze(1),
                        return_dict=False,
                    )[0]

                cross_attention_kwargs = {}
                cross_attention_kwargs["gligen"] = {
                    "boxes": batch["boxes"],
                    "positive_embeddings": batch["text_embeddings_before_projection"],
                    "masks": batch["masks"],
                }
                # Predict the noise residual
                model_pred = unet(
                    noisy_latents,
                    timesteps,
                    encoder_hidden_states=encoder_hidden_states,
                    cross_attention_kwargs=cross_attention_kwargs,
                    return_dict=False,
                )[0]

                # Get the target for loss depending on the prediction type
                if noise_scheduler.config.prediction_type == "epsilon":
                    target = noise
                elif noise_scheduler.config.prediction_type == "v_prediction":
                    target = noise_scheduler.get_velocity(latents, noise, timesteps)
                else:
                    raise ValueError(f"Unknown prediction type {noise_scheduler.config.prediction_type}")
                loss = F.mse_loss(model_pred.float(), target.float(), reduction="mean")

                accelerator.backward(loss)
                if accelerator.sync_gradients:
                    accelerator.clip_grad_norm_(params_to_optimize, args.max_grad_norm)
                optimizer.step()
                lr_scheduler.step()
                optimizer.zero_grad(set_to_none=args.set_grads_to_none)

            # Checks if the accelerator has performed an optimization step behind the scenes
            if accelerator.sync_gradients:
                progress_bar.update(1)
                global_step += 1

                if global_step % args.checkpointing_steps == 0:
                    if accelerator.is_main_process:
                        # _before_ saving state, check if this save would set us over the `checkpoints_total_limit`
                        if args.checkpoints_total_limit is not None:
                            checkpoints = os.listdir(args.output_dir)
                            checkpoints = [d for d in checkpoints if d.startswith("checkpoint")]
                            checkpoints = sorted(checkpoints, key=lambda x: int(x.split("-")[1]))

                            # before we save the new checkpoint, we need to have at _most_ `checkpoints_total_limit - 1` checkpoints
                            if len(checkpoints) >= args.checkpoints_total_limit:
                                num_to_remove = len(checkpoints) - args.checkpoints_total_limit + 1
                                removing_checkpoints = checkpoints[0:num_to_remove]

                                logger.info(
                                    f"{len(checkpoints)} checkpoints already exist, removing {len(removing_checkpoints)} checkpoints"
                                )
                                logger.info(f"removing checkpoints: {', '.join(removing_checkpoints)}")

                                for removing_checkpoint in removing_checkpoints:
                                    removing_checkpoint = os.path.join(args.output_dir, removing_checkpoint)
                                    shutil.rmtree(removing_checkpoint)

                        save_path = os.path.join(args.output_dir, f"checkpoint-{global_step:06d}")
                        accelerator.save_state(save_path)
                        logger.info(f"Saved state to {save_path}")

                    # if args.validation_prompt is not None and global_step % args.validation_steps == 0:
                    log_validation(
                        vae,
                        text_encoder,
                        tokenizer,
                        unet,
                        noise_scheduler,
                        args,
                        accelerator,
                        global_step,
                        weight_dtype,
                       args.data_path_validation,
                    )
            logs = {"loss": loss.detach().item(), "lr": lr_scheduler.get_last_lr()[0]}
            progress_bar.set_postfix(**logs)
            accelerator.log(logs, step=global_step)

            if global_step >= args.max_train_steps:
                break

    # Create the pipeline using using the trained modules and save it.
    accelerator.wait_for_everyone()
    if accelerator.is_main_process:
        unet = unwrap_model(unet)
        unet.save_pretrained(args.output_dir)
    #
    #     # Run a final round of validation.
    #     image_logs = None
    #     if args.validation_prompt is not None:
    #         image_logs = log_validation(
    #             vae=vae,
    #             text_encoder=text_encoder,
    #             tokenizer=tokenizer,
    #             unet=unet,
    #             controlnet=None,
    #             args=args,
    #             accelerator=accelerator,
    #             weight_dtype=weight_dtype,
    #             step=global_step,
    #             is_final_validation=True,
    #         )
    #
    #     if args.push_to_hub:
    #         save_model_card(
    #             repo_id,
    #             image_logs=image_logs,
    #             base_model=args.pretrained_model_name_or_path,
    #             repo_folder=args.output_dir,
    #         )
    #         upload_folder(
    #             repo_id=repo_id,
    #             folder_path=args.output_dir,
    #             commit_message="End of training",
    #             ignore_patterns=["step_*", "epoch_*"],
    #         )

    accelerator.end_training()


if __name__ == "__main__":
    print("starting")
    #https://huggingface.co/gligen/diffusers-generation-text-box
    args = parse_args()
    import os
    import platform

    # Check if the operating system is Windows and disable triton (because that kills everything)
    if os.name == 'nt' or platform.system() == 'Windows':
        print("Running on Windows disable triton")
        import torch._dynamo
        torch._dynamo.config.suppress_errors = True
    else:
        import multiprocessing
        multiprocessing.set_start_method('spawn')
    main(args)
