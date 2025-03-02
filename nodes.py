import math
import torch

from torch import Tensor
from unittest.mock import patch

from comfy.ldm.flux.layers import timestep_embedding
from comfy.ldm.lightricks.model import precompute_freqs_cis
from comfy.ldm.common_dit import rms_norm
from comfy.ldm.wan.model import sinusoidal_embedding_1d


def poly1d(coefficients, x):
    result = torch.zeros_like(x)
    for i, coeff in enumerate(coefficients):
        result += coeff * (x ** (len(coefficients) - 1 - i))
    return result

def relative_l1_distance(last_tensor, current_tensor):
    l1_distance = torch.abs(last_tensor - current_tensor).mean()
    norm = torch.abs(last_tensor).mean()
    relative_l1_distance = l1_distance / norm
    return relative_l1_distance.to(torch.float32)

def teacache_flux_forward(
        self,
        img: Tensor,
        img_ids: Tensor,
        txt: Tensor,
        txt_ids: Tensor,
        timesteps: Tensor,
        y: Tensor,
        guidance: Tensor = None,
        control = None,
        transformer_options={},
        attn_mask: Tensor = None,
    ) -> Tensor:
        patches_replace = transformer_options.get("patches_replace", {})
        rel_l1_thresh = transformer_options.get("rel_l1_thresh", {})
        
        if img.ndim != 3 or txt.ndim != 3:
            raise ValueError("Input img and txt tensors must have 3 dimensions.")

        # running on sequences img
        img = self.img_in(img)
        vec = self.time_in(timestep_embedding(timesteps, 256).to(img.dtype))
        if self.params.guidance_embed:
            if guidance is None:
                raise ValueError("Didn't get guidance strength for guidance distilled model.")
            vec = vec + self.guidance_in(timestep_embedding(guidance, 256).to(img.dtype))

        vec = vec + self.vector_in(y[:,:self.params.vec_in_dim])
        txt = self.txt_in(txt)

        ids = torch.cat((txt_ids, img_ids), dim=1)
        pe = self.pe_embedder(ids)

        blocks_replace = patches_replace.get("dit", {})

        # enable teacache
        inp = img.clone()
        vec_ = vec.clone()
        img_mod1, _ = self.double_blocks[0].img_mod(vec_)
        modulated_inp = self.double_blocks[0].img_norm1(inp)
        modulated_inp = (1 + img_mod1.scale) * modulated_inp + img_mod1.shift
        ca_idx = 0

        if not hasattr(self, 'accumulated_rel_l1_distance'):
            should_calc = True
            self.accumulated_rel_l1_distance = 0
        else:
            try:
                coefficients = [4.98651651e+02, -2.83781631e+02, 5.58554382e+01, -3.82021401e+00, 2.64230861e-01]
                self.accumulated_rel_l1_distance += poly1d(coefficients, ((modulated_inp-self.previous_modulated_input).abs().mean() / self.previous_modulated_input.abs().mean()))
                if self.accumulated_rel_l1_distance < rel_l1_thresh:
                    should_calc = False
                else:
                    should_calc = True
                    self.accumulated_rel_l1_distance = 0
            except:
                should_calc = True
                self.accumulated_rel_l1_distance = 0

        self.previous_modulated_input = modulated_inp

        if not should_calc:
            img += self.previous_residual
        else:
            ori_img = img.clone()
            for i, block in enumerate(self.double_blocks):
                if ("double_block", i) in blocks_replace:
                    def block_wrap(args):
                        out = {}
                        out["img"], out["txt"] = block(img=args["img"],
                                                    txt=args["txt"],
                                                    vec=args["vec"],
                                                    pe=args["pe"],
                                                    attn_mask=args.get("attn_mask"))
                        return out

                    out = blocks_replace[("double_block", i)]({"img": img,
                                                            "txt": txt,
                                                            "vec": vec,
                                                            "pe": pe,
                                                            "attn_mask": attn_mask},
                                                            {"original_block": block_wrap})
                    txt = out["txt"]
                    img = out["img"]
                else:
                    img, txt = block(img=img,
                                    txt=txt,
                                    vec=vec,
                                    pe=pe,
                                    attn_mask=attn_mask)

                if control is not None: # Controlnet
                    control_i = control.get("input")
                    if i < len(control_i):
                        add = control_i[i]
                        if add is not None:
                            img += add

                # PuLID attention
                if getattr(self, "pulid_data", {}):
                    if i % self.pulid_double_interval == 0:
                        # Will calculate influence of all pulid nodes at once
                        for _, node_data in self.pulid_data.items():
                            if torch.any((node_data['sigma_start'] >= timesteps)
                                        & (timesteps >= node_data['sigma_end'])):
                                img = img + node_data['weight'] * self.pulid_ca[ca_idx](node_data['embedding'], img)
                        ca_idx += 1

            img = torch.cat((txt, img), 1)

            for i, block in enumerate(self.single_blocks):
                if ("single_block", i) in blocks_replace:
                    def block_wrap(args):
                        out = {}
                        out["img"] = block(args["img"],
                                        vec=args["vec"],
                                        pe=args["pe"],
                                        attn_mask=args.get("attn_mask"))
                        return out

                    out = blocks_replace[("single_block", i)]({"img": img,
                                                            "vec": vec,
                                                            "pe": pe,
                                                            "attn_mask": attn_mask}, 
                                                            {"original_block": block_wrap})
                    img = out["img"]
                else:
                    img = block(img, vec=vec, pe=pe, attn_mask=attn_mask)

                if control is not None: # Controlnet
                    control_o = control.get("output")
                    if i < len(control_o):
                        add = control_o[i]
                        if add is not None:
                            img[:, txt.shape[1] :, ...] += add

                # PuLID attention
                if getattr(self, "pulid_data", {}):
                    real_img, txt = img[:, txt.shape[1]:, ...], img[:, :txt.shape[1], ...]
                    if i % self.pulid_single_interval == 0:
                        # Will calculate influence of all nodes at once
                        for _, node_data in self.pulid_data.items():
                            if torch.any((node_data['sigma_start'] >= timesteps)
                                        & (timesteps >= node_data['sigma_end'])):
                                real_img = real_img + node_data['weight'] * self.pulid_ca[ca_idx](node_data['embedding'], real_img)
                        ca_idx += 1
                    img = torch.cat((txt, real_img), 1)

            img = img[:, txt.shape[1] :, ...]
            self.previous_residual = img - ori_img

        img = self.final_layer(img, vec)  # (N, T, patch_size ** 2 * out_channels)
        
        return img

def teacache_hunyuanvideo_forward(
        self,
        img: Tensor,
        img_ids: Tensor,
        txt: Tensor,
        txt_ids: Tensor,
        txt_mask: Tensor,
        timesteps: Tensor,
        y: Tensor,
        guidance: Tensor = None,
        control=None,
        transformer_options={},
    ) -> Tensor:
        patches_replace = transformer_options.get("patches_replace", {})
        rel_l1_thresh = transformer_options.get("rel_l1_thresh", {})

        initial_shape = list(img.shape)
        # running on sequences img
        img = self.img_in(img)
        vec = self.time_in(timestep_embedding(timesteps, 256, time_factor=1.0).to(img.dtype))

        vec = vec + self.vector_in(y[:, :self.params.vec_in_dim])

        if self.params.guidance_embed:
            if guidance is None:
                raise ValueError("Didn't get guidance strength for guidance distilled model.")
            vec = vec + self.guidance_in(timestep_embedding(guidance, 256).to(img.dtype))

        if txt_mask is not None and not torch.is_floating_point(txt_mask):
            txt_mask = (txt_mask - 1).to(img.dtype) * torch.finfo(img.dtype).max

        txt = self.txt_in(txt, timesteps, txt_mask)

        ids = torch.cat((img_ids, txt_ids), dim=1)
        pe = self.pe_embedder(ids)

        img_len = img.shape[1]
        if txt_mask is not None:
            attn_mask_len = img_len + txt.shape[1]
            attn_mask = torch.zeros((1, 1, attn_mask_len), dtype=img.dtype, device=img.device)
            attn_mask[:, 0, img_len:] = txt_mask
        else:
            attn_mask = None

        blocks_replace = patches_replace.get("dit", {})

        # enable teacache
        inp = img.clone()
        vec_ = vec.clone()
        img_mod1, _ = self.double_blocks[0].img_mod(vec_)
        modulated_inp = self.double_blocks[0].img_norm1(inp)
        modulated_inp = (1 + img_mod1.scale) * modulated_inp + img_mod1.shift

        if not hasattr(self, 'accumulated_rel_l1_distance'):
            should_calc = True
            self.accumulated_rel_l1_distance = 0
        else:
            try:
                coefficients = [7.33226126e+02, -4.01131952e+02, 6.75869174e+01, -3.14987800e+00, 9.61237896e-02]
                self.accumulated_rel_l1_distance += poly1d(coefficients, ((modulated_inp-self.previous_modulated_input).abs().mean() / self.previous_modulated_input.abs().mean()))
                if self.accumulated_rel_l1_distance < rel_l1_thresh:
                    should_calc = False
                else:
                    should_calc = True
                    self.accumulated_rel_l1_distance = 0
            except:
                should_calc = True
                self.accumulated_rel_l1_distance = 0

        self.previous_modulated_input = modulated_inp 

        if not should_calc:
            img += self.previous_residual
        else:
            ori_img = img.clone()
            for i, block in enumerate(self.double_blocks):
                if ("double_block", i) in blocks_replace:
                    def block_wrap(args):
                        out = {}
                        out["img"], out["txt"] = block(img=args["img"], txt=args["txt"], vec=args["vec"], pe=args["pe"], attn_mask=args["attention_mask"])
                        return out

                    out = blocks_replace[("double_block", i)]({"img": img, "txt": txt, "vec": vec, "pe": pe, "attention_mask": attn_mask}, {"original_block": block_wrap})
                    txt = out["txt"]
                    img = out["img"]
                else:
                    img, txt = block(img=img, txt=txt, vec=vec, pe=pe, attn_mask=attn_mask)

                if control is not None: # Controlnet
                    control_i = control.get("input")
                    if i < len(control_i):
                        add = control_i[i]
                        if add is not None:
                            img += add

            img = torch.cat((img, txt), 1)

            for i, block in enumerate(self.single_blocks):
                if ("single_block", i) in blocks_replace:
                    def block_wrap(args):
                        out = {}
                        out["img"] = block(args["img"], vec=args["vec"], pe=args["pe"], attn_mask=args["attention_mask"])
                        return out

                    out = blocks_replace[("single_block", i)]({"img": img, "vec": vec, "pe": pe, "attention_mask": attn_mask}, {"original_block": block_wrap})
                    img = out["img"]
                else:
                    img = block(img, vec=vec, pe=pe, attn_mask=attn_mask)

                if control is not None: # Controlnet
                    control_o = control.get("output")
                    if i < len(control_o):
                        add = control_o[i]
                        if add is not None:
                            img[:, : img_len] += add

            img = img[:, : img_len]
            self.previous_residual = img - ori_img

        img = self.final_layer(img, vec)  # (N, T, patch_size ** 2 * out_channels)

        shape = initial_shape[-3:]
        for i in range(len(shape)):
            shape[i] = shape[i] // self.patch_size[i]
        img = img.reshape([img.shape[0]] + shape + [self.out_channels] + self.patch_size)
        img = img.permute(0, 4, 1, 5, 2, 6, 3, 7)
        img = img.reshape(initial_shape[0], self.out_channels, initial_shape[2], initial_shape[3], initial_shape[4])
        return img

def teacache_ltxvmodel_forward(
        self,
        x,
        timestep,
        context,
        attention_mask,
        frame_rate=25,
        guiding_latent=None,
        guiding_latent_noise_scale=0,
        transformer_options={},
        **kwargs
    ):
        patches_replace = transformer_options.get("patches_replace", {})
        rel_l1_thresh = transformer_options.get("rel_l1_thresh", {})

        indices_grid = self.patchifier.get_grid(
            orig_num_frames=x.shape[2],
            orig_height=x.shape[3],
            orig_width=x.shape[4],
            batch_size=x.shape[0],
            scale_grid=((1 / frame_rate) * 8, 32, 32),
            device=x.device,
        )

        if guiding_latent is not None:
            ts = torch.ones([x.shape[0], 1, x.shape[2], x.shape[3], x.shape[4]], device=x.device, dtype=x.dtype)
            input_ts = timestep.view([timestep.shape[0]] + [1] * (x.ndim - 1))
            ts *= input_ts
            ts[:, :, 0] = guiding_latent_noise_scale * (input_ts[:, :, 0] ** 2)
            timestep = self.patchifier.patchify(ts)
            input_x = x.clone()
            x[:, :, 0] = guiding_latent[:, :, 0]
            if guiding_latent_noise_scale > 0:
                if self.generator is None:
                    self.generator = torch.Generator(device=x.device).manual_seed(42)
                elif self.generator.device != x.device:
                    self.generator = torch.Generator(device=x.device).set_state(self.generator.get_state())

                noise_shape = [guiding_latent.shape[0], guiding_latent.shape[1], 1, guiding_latent.shape[3], guiding_latent.shape[4]]
                scale = guiding_latent_noise_scale * (input_ts ** 2)
                guiding_noise = scale * torch.randn(size=noise_shape, device=x.device, generator=self.generator)

                x[:, :, 0] = guiding_noise[:, :, 0] + x[:, :, 0] *  (1.0 - scale[:, :, 0])


        orig_shape = list(x.shape)

        x = self.patchifier.patchify(x)

        x = self.patchify_proj(x)
        timestep = timestep * 1000.0

        attention_mask = 1.0 - attention_mask.to(x.dtype).reshape((attention_mask.shape[0], 1, -1, attention_mask.shape[-1]))
        attention_mask = attention_mask.masked_fill(attention_mask.to(torch.bool), float("-inf"))  # not sure about this
        # attention_mask = (context != 0).any(dim=2).to(dtype=x.dtype)

        pe = precompute_freqs_cis(indices_grid, dim=self.inner_dim, out_dtype=x.dtype)

        batch_size = x.shape[0]
        timestep, embedded_timestep = self.adaln_single(
            timestep.flatten(),
            {"resolution": None, "aspect_ratio": None},
            batch_size=batch_size,
            hidden_dtype=x.dtype,
        )
        # Second dimension is 1 or number of tokens (if timestep_per_token)
        timestep = timestep.view(batch_size, -1, timestep.shape[-1])
        embedded_timestep = embedded_timestep.view(
            batch_size, -1, embedded_timestep.shape[-1]
        )

        # 2. Blocks
        if self.caption_projection is not None:
            batch_size = x.shape[0]
            context = self.caption_projection(context)
            context = context.view(
                batch_size, -1, x.shape[-1]
            )

        blocks_replace = patches_replace.get("dit", {})

        # enable teacache
        inp = x.clone()
        timestep_ = timestep.clone()
        num_ada_params = self.transformer_blocks[0].scale_shift_table.shape[0]
        ada_values = self.transformer_blocks[0].scale_shift_table[None, None] + timestep_.reshape(batch_size, timestep_.size(1), num_ada_params, -1)
        shift_msa, scale_msa, _, _, _, _ = ada_values.unbind(dim=2)
        modulated_inp = rms_norm(inp)
        modulated_inp = modulated_inp * (1 + scale_msa) + shift_msa
        
        if not hasattr(self, 'accumulated_rel_l1_distance'):
            should_calc = True
            self.accumulated_rel_l1_distance = 0
        else:
            try:
                coefficients = [2.14700694e+01, -1.28016453e+01, 2.31279151e+00, 7.92487521e-01, 9.69274326e-03]
                self.accumulated_rel_l1_distance += poly1d(coefficients, ((modulated_inp-self.previous_modulated_input).abs().mean() / self.previous_modulated_input.abs().mean()))
                if self.accumulated_rel_l1_distance < rel_l1_thresh:
                    should_calc = False
                else:
                    should_calc = True
                    self.accumulated_rel_l1_distance = 0
            except:
                should_calc = True
                self.accumulated_rel_l1_distance = 0
                
        self.previous_modulated_input = modulated_inp

        
        if not should_calc:
            x += self.previous_residual
        else:
            ori_x = x.clone()
            for i, block in enumerate(self.transformer_blocks):
                if ("double_block", i) in blocks_replace:
                    def block_wrap(args):
                        out = {}
                        out["img"] = block(args["img"], context=args["txt"], attention_mask=args["attention_mask"], timestep=args["vec"], pe=args["pe"])
                        return out

                    out = blocks_replace[("double_block", i)]({"img": x, "txt": context, "attention_mask": attention_mask, "vec": timestep, "pe": pe}, {"original_block": block_wrap})
                    x = out["img"]
                else:
                    x = block(
                        x,
                        context=context,
                        attention_mask=attention_mask,
                        timestep=timestep,
                        pe=pe
                    )

            # 3. Output
            scale_shift_values = (
                self.scale_shift_table[None, None].to(device=x.device, dtype=x.dtype) + embedded_timestep[:, :, None]
            )
            shift, scale = scale_shift_values[:, :, 0], scale_shift_values[:, :, 1]
            x = self.norm_out(x)
            # Modulation
            x = x * (1 + scale) + shift
            self.previous_residual = x - ori_x

        x = self.proj_out(x)

        x = self.patchifier.unpatchify(
            latents=x,
            output_height=orig_shape[3],
            output_width=orig_shape[4],
            output_num_frames=orig_shape[2],
            out_channels=orig_shape[1] // math.prod(self.patchifier.patch_size),
        )

        if guiding_latent is not None:
            x[:, :, 0] = (input_x[:, :, 0] - guiding_latent[:, :, 0]) / input_ts[:, :, 0]

        # print("res", x)
        return x



def teacache_wanvideo_forward(
        self,
        x,
        t,
        context,
        clip_fea=None,
        freqs=None,
    ):
        r"""
        Forward pass through the diffusion model

        Args:
            x (Tensor):
                List of input video tensors with shape [B, C_in, F, H, W]
            t (Tensor):
                Diffusion timesteps tensor of shape [B]
            context (List[Tensor]):
                List of text embeddings each with shape [B, L, C]
            seq_len (`int`):
                Maximum sequence length for positional encoding
            clip_fea (Tensor, *optional*):
                CLIP image features for image-to-video mode
            y (List[Tensor], *optional*):
                Conditional video inputs for image-to-video mode, same shape as x

        Returns:
            List[Tensor]:
                List of denoised video tensors with original input shapes [C_out, F, H / 8, W / 8]
        """
        
        # embeddings
        x = self.patch_embedding(x.float()).to(x.dtype)
        grid_sizes = x.shape[2:]
        x = x.flatten(2).transpose(1, 2)

        # time embeddings
        e = self.time_embedding(
            sinusoidal_embedding_1d(self.freq_dim, t).to(dtype=x[0].dtype))
        e0 = self.time_projection(e).unflatten(1, (6, self.dim))

        # context
        context = self.text_embedding(context)

        if clip_fea is not None and self.img_emb is not None:
            context_clip = self.img_emb(clip_fea)  # bs x 257 x dim
            context = torch.concat([context_clip, context], dim=1)

        rel_l1_thresh = self.rel_l1_thresh
        print(rel_l1_thresh)
        
        if not hasattr(self, 'accumulated_rel_l1_distance'):
            should_calc = True
            self.accumulated_rel_l1_distance = 0
            print("TeaCache: Initializing TeaCache variables")
        else:
            temb_relative_l1 = relative_l1_distance(self.previous_modulated_input, e0)
            self.accumulated_rel_l1_distance += temb_relative_l1
            try:
                if self.accumulated_rel_l1_distance < rel_l1_thresh:
                    should_calc = False
                else:
                    should_calc = True
                    self.accumulated_rel_l1_distance = 0
            except:
                should_calc = True
                self.accumulated_rel_l1_distance = 0

        self.previous_modulated_input = e0.clone()

        if not should_calc:
            x += self.previous_residual
            print(f"TeaCache: Skipping step")

        if should_calc:
            original_x = x.clone()
            # arguments
            kwargs = dict(
                e=e0,
                freqs=freqs,
                context=context)

            for block in self.blocks:
                x = block(x, **kwargs)

            self.previous_residual = (x - original_x)

        # head
        x = self.head(x, e)

        # unpatchify
        x = self.unpatchify(x, grid_sizes)
        return x

class TeaCacheForImgGen:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "model": ("MODEL", {"tooltip": "The image diffusion model the TeaCache will be applied to."}),
                "model_type": (["flux"],),
                "rel_l1_thresh": ("FLOAT", {"default": 0.4, "min": 0.0, "max": 10.0, "step": 0.01, "tooltip": "How strongly to cache the output of diffusion model. This value must be non-negative."})
            }
        }
    
    RETURN_TYPES = ("MODEL",)
    RETURN_NAMES = ("model",)
    FUNCTION = "apply_teacache"
    CATEGORY = "TeaCache"
    TITLE = "TeaCache For Img Gen"
    
    def apply_teacache(self, model, model_type: str, rel_l1_thresh: float):
        if rel_l1_thresh == 0:
            return (model,)

        new_model = model.clone()
        if 'transformer_options' not in new_model.model_options:
            new_model.model_options['transformer_options'] = {}
        new_model.model_options["transformer_options"]["rel_l1_thresh"] = rel_l1_thresh
        diffusion_model = new_model.get_model_object("diffusion_model")

        if model_type == "flux":
            forward_name = "forward_orig"
            replaced_forward_fn = teacache_flux_forward.__get__(
                                diffusion_model,
                                diffusion_model.__class__
                            )
        else:
            raise ValueError(f"Unknown type {model_type}")
        
        def unet_wrapper_function(model_function, kwargs):
            input = kwargs["input"]
            timestep = kwargs["timestep"]
            c = kwargs["c"]
            with patch.object(diffusion_model, forward_name, replaced_forward_fn):
                return model_function(input, timestep, **c)

        new_model.set_model_unet_function_wrapper(unet_wrapper_function)
        
        return (new_model,)
    
class TeaCacheForVidGen:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "model": ("MODEL", {"tooltip": "The video diffusion model the TeaCache will be applied to."}),
                "model_type": (["hunyuan_video", "ltxv", "wan_video"],),
                "rel_l1_thresh": ("FLOAT", {"default": 0.15, "min": 0.0, "max": 10.0, "step": 0.01, "tooltip": "How strongly to cache the output of diffusion model. This value must be non-negative."})
            }
        }
    
    RETURN_TYPES = ("MODEL",)
    RETURN_NAMES = ("model",)
    FUNCTION = "apply_teacache"
    CATEGORY = "TeaCache"
    TITLE = "TeaCache For Vid Gen"
    
    def apply_teacache(self, model, model_type: str, rel_l1_thresh: float):
        if rel_l1_thresh == 0:
            return (model,)

        new_model = model.clone()
        if 'transformer_options' not in new_model.model_options:
            new_model.model_options['transformer_options'] = {}
        new_model.model_options["transformer_options"]["rel_l1_thresh"] = rel_l1_thresh
        diffusion_model = new_model.get_model_object("diffusion_model")
        diffusion_model.rel_l1_thresh = rel_l1_thresh

        if model_type == "hunyuan_video":
            forward_name = "forward_orig"
            replaced_forward_fn = teacache_hunyuanvideo_forward.__get__(
                                diffusion_model,
                                diffusion_model.__class__
                            )
        elif model_type == "ltxv":
            forward_name = "forward"
            replaced_forward_fn = teacache_ltxvmodel_forward.__get__(
                                diffusion_model,
                                diffusion_model.__class__
                            )
        elif model_type == "wan_video":
            forward_name = "forward_orig"
            replaced_forward_fn = teacache_wanvideo_forward.__get__(
                                diffusion_model,
                                diffusion_model.__class__
            )
        else:
            raise ValueError(f"Unknown type {model_type}")
        
        def unet_wrapper_function(model_function, kwargs):
            input = kwargs["input"]
            timestep = kwargs["timestep"]
            c = kwargs["c"]
            with patch.object(diffusion_model, forward_name, replaced_forward_fn):
                return model_function(input, timestep, **c)

        new_model.set_model_unet_function_wrapper(unet_wrapper_function)

        return (new_model,)
    
def patch_optimized_module():
    try:
        from torch._dynamo.eval_frame import OptimizedModule
    except ImportError:
        return

    if getattr(OptimizedModule, "_patched", False):
        return

    def __getattribute__(self, name):
        if name == "_orig_mod":
            return object.__getattribute__(self, "_modules")[name]
        if name in (
            "__class__",
            "_modules",
            "state_dict",
            "load_state_dict",
            "parameters",
            "named_parameters",
            "buffers",
            "named_buffers",
            "children",
            "named_children",
            "modules",
            "named_modules",
        ):
            return getattr(object.__getattribute__(self, "_orig_mod"), name)
        return object.__getattribute__(self, name)

    def __delattr__(self, name):
        return delattr(self._orig_mod, name)

    @classmethod
    def __instancecheck__(cls, instance):
        return isinstance(instance, OptimizedModule) or issubclass(
            object.__getattribute__(instance, "__class__"), cls
        )

    OptimizedModule.__getattribute__ = __getattribute__
    OptimizedModule.__delattr__ = __delattr__
    OptimizedModule.__instancecheck__ = __instancecheck__
    OptimizedModule._patched = True

def patch_same_meta():
    try:
        from torch._inductor.fx_passes import post_grad
    except ImportError:
        return

    same_meta = getattr(post_grad, "same_meta", None)
    if same_meta is None:
        return

    if getattr(same_meta, "_patched", False):
        return

    def new_same_meta(a, b):
        try:
            return same_meta(a, b)
        except Exception:
            return False

    post_grad.same_meta = new_same_meta
    new_same_meta._patched = True

class CompileModel:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "model": ("MODEL", {"tooltip": "The diffusion model the torch.compile will be applied to."}),
                "mode": (["default", "max-autotune", "max-autotune-no-cudagraphs", "reduce-overhead"], {"default": "default"}),
                "backend": (["inductor","cudagraphs", "eager", "aot_eager"], {"default": "inductor"}),
                "fullgraph": ("BOOLEAN", {"default": False, "tooltip": "Enable full graph mode"}),
                "dynamic": ("BOOLEAN", {"default": False, "tooltip": "Enable dynamic mode"}),
            }
        }
    
    RETURN_TYPES = ("MODEL",)
    RETURN_NAMES = ("model",)
    FUNCTION = "apply_compile"
    CATEGORY = "TeaCache"
    TITLE = "Compile Model"
    
    def apply_compile(self, model, mode: str, backend: str, fullgraph: bool, dynamic: bool):
        patch_optimized_module()
        patch_same_meta()
        torch._dynamo.config.suppress_errors = True
        
        new_model = model.clone()
        new_model.add_object_patch(
                                "diffusion_model",
                                torch.compile(
                                    new_model.get_model_object("diffusion_model"),
                                    mode=mode,
                                    backend=backend,
                                    fullgraph=fullgraph,
                                    dynamic=dynamic
                                )
                            )
        
        return (new_model,)
    

NODE_CLASS_MAPPINGS = {
    "TeaCacheForImgGen": TeaCacheForImgGen,
    "TeaCacheForVidGen": TeaCacheForVidGen,
    "CompileModel": CompileModel
}

NODE_DISPLAY_NAME_MAPPINGS = {k: v.TITLE for k, v in NODE_CLASS_MAPPINGS.items()}
