"""Submit the official ComfyUI 'video_wan2_2_14B_i2v' template (flattened to API format,
default non-turbo path) with the article's step-4 settings, then wait and print the output path.

Usage: python wan_idle.py [--image NAME_IN_ComfyUI/input] [--prefix video/idle] [--prompt TEXT] [--neg-extra TEXT] [--seed N]"""
import argparse, json, sys, time, urllib.request, random

HOST = "http://127.0.0.1:8188"
# Article step 4 prompt (actions only, no appearance description)
PROMPT = "她保持静止，轻微呼吸起伏；中途缓慢眨一次眼； 结尾头部回到最初位置。嘴唇始终闭合。固定机位。"
# Template default negative prompt
NEG = ("色调艳丽，过曝，静态，细节模糊不清，字幕，风格，作品，画作，画面，静止，整体发灰，最差质量，低质量，JPEG压缩残留，"
       "丑陋的，残缺的，多余的手指，画得不好的手部，画得不好的脸部，畸形的，毁容的，形态畸形的肢体，手指融合，静止不动的画面，"
       "杂乱的背景，三条腿，背景人很多，倒着走")
W, H, DURATION, FPS = 704, 896, 5, 16          # article: 704x896, 5 s; template fps 16 -> 81 frames
LENGTH = int(DURATION * FPS + 1)
STEPS, CFG, SPLIT = 20, 3.5, 10                  # template defaults (enable_turbo_mode = false)
ap = argparse.ArgumentParser()
ap.add_argument("--image", default="xiaoya_avatar.png")
ap.add_argument("--prefix", default="video/idle")
ap.add_argument("--prompt", default=PROMPT)
ap.add_argument("--neg-extra", default="", help="appended to the template negative prompt")
ap.add_argument("--seed", type=int, default=None)
args = ap.parse_args()
IMAGE, PREFIX, PROMPT = args.image, args.prefix, args.prompt
if args.neg_extra:
    NEG = NEG + "，" + args.neg_extra
seed = args.seed if args.seed is not None else random.randint(0, 2**48)

g = {
    "1": {"class_type": "LoadImage", "inputs": {"image": IMAGE}},
    "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "umt5_xxl_fp8_e4m3fn_scaled.safetensors", "type": "wan", "device": "default"}},
    "3": {"class_type": "VAELoader", "inputs": {"vae_name": "wan_2.1_vae.safetensors"}},
    "4": {"class_type": "UNETLoader", "inputs": {"unet_name": "wan2.2_i2v_high_noise_14B_fp8_scaled.safetensors", "weight_dtype": "default"}},
    "5": {"class_type": "UNETLoader", "inputs": {"unet_name": "wan2.2_i2v_low_noise_14B_fp8_scaled.safetensors", "weight_dtype": "default"}},
    "6": {"class_type": "ModelSamplingSD3", "inputs": {"model": ["4", 0], "shift": 5.0}},
    "7": {"class_type": "ModelSamplingSD3", "inputs": {"model": ["5", 0], "shift": 5.0}},
    "8": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": PROMPT}},
    "9": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": NEG}},
    "10": {"class_type": "WanImageToVideo", "inputs": {"positive": ["8", 0], "negative": ["9", 0], "vae": ["3", 0],
            "start_image": ["1", 0], "width": W, "height": H, "length": LENGTH, "batch_size": 1}},
    "11": {"class_type": "KSamplerAdvanced", "inputs": {"model": ["6", 0], "positive": ["10", 0], "negative": ["10", 1],
            "latent_image": ["10", 2], "add_noise": "enable", "noise_seed": seed, "steps": STEPS, "cfg": CFG,
            "sampler_name": "euler", "scheduler": "simple", "start_at_step": 0, "end_at_step": SPLIT,
            "return_with_leftover_noise": "enable"}},
    "12": {"class_type": "KSamplerAdvanced", "inputs": {"model": ["7", 0], "positive": ["10", 0], "negative": ["10", 1],
            "latent_image": ["11", 0], "add_noise": "disable", "noise_seed": 0, "steps": STEPS, "cfg": CFG,
            "sampler_name": "euler", "scheduler": "simple", "start_at_step": SPLIT, "end_at_step": 10000,
            "return_with_leftover_noise": "disable"}},
    "13": {"class_type": "VAEDecode", "inputs": {"samples": ["12", 0], "vae": ["3", 0]}},
    "14": {"class_type": "CreateVideo", "inputs": {"images": ["13", 0], "fps": float(FPS)}},
    "15": {"class_type": "SaveVideo", "inputs": {"video": ["14", 0], "filename_prefix": PREFIX, "format": "auto", "codec": "auto"}},
}

req = urllib.request.Request(f"{HOST}/prompt", data=json.dumps({"prompt": g}).encode(), headers={"Content-Type": "application/json"})
try:
    pid = json.load(urllib.request.urlopen(req))["prompt_id"]
except urllib.error.HTTPError as e:
    print(e.read().decode()); sys.exit(1)
print("queued", pid, "seed", seed, flush=True)
t0 = time.time()
while True:
    time.sleep(10)
    h = json.load(urllib.request.urlopen(f"{HOST}/history/{pid}"))
    if pid in h:
        st = h[pid]["status"]
        print("status", st.get("status_str"), f"{time.time()-t0:.0f}s")
        for out in h[pid]["outputs"].values():
            print(json.dumps(out, ensure_ascii=False))
        if st.get("status_str") != "success":
            print(json.dumps(st.get("messages"), ensure_ascii=False)[-3000:])
        break
