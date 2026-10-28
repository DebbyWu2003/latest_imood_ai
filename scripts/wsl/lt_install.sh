#!/usr/bin/env bash
# LiveTalking install, wrapped in retries for flaky networks.
# Already executed successfully on 2026-09-25 — do NOT re-run unless reinstalling.
set -ex
export PATH="$HOME/.local/bin:$PATH" UV_HTTP_TIMEOUT=300 UV_HTTP_RETRIES=10
retry(){ for i in $(seq 1 15); do "$@" && return 0; echo "retry $i: $*"; sleep 5; done; return 1; }
sudo apt update && sudo apt install -y dos2unix ffmpeg libgl1 libglib2.0-0
command -v uv || curl -LsSf https://astral.sh/uv/install.sh | sh
mkdir -p ~/livetalking && cd ~/livetalking
if [ ! -d LiveTalking/.git ]; then rm -rf LiveTalking; retry git clone https://github.com/lipku/LiveTalking.git; fi
cd LiveTalking
git log -1 --format='COMMIT %H %cd'
[ -d .venv-lt ] || uv venv --python 3.10 .venv-lt
source .venv-lt/bin/activate
retry uv pip install torch==2.5.0 torchvision==0.20.0 torchaudio==2.5.0 \
  --index-url https://download.pytorch.org/whl/cu124
retry uv pip install -r requirements.txt
python -c "import torch;print('TORCH',torch.__version__,torch.cuda.is_available(),torch.cuda.get_device_name(0))"
echo LT_INSTALL_DONE
