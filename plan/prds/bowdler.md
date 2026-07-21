Quickstart
Install dependencies, build bowdler, and produce your first family-friendly cut.

On this page

Hardware requirements
System dependencies
Build
Models
NudeNet 320n — manual fallback
Whisper — manual fallback
Your first cut
Hardware requirements
The pipeline assumes one specific shape: NVIDIA GPU + recent CUDA/Vulkan + modern x86 Linux. There is no CPU fallback for video decode/inference.

System dependencies
Arch package names — translate as appropriate for your distribution.

# Core
sudo pacman -S --needed base-devel cmake clang pkgconf
sudo pacman -S --needed cuda cudnn          # NVDEC + CUDA EP
sudo pacman -S --needed vulkan-headers vulkan-icd-loader
                                            # Vulkan compute (whisper.cpp backend)
sudo pacman -S --needed ffmpeg              # 8.x with --enable-nvdec --enable-nvenc
sudo pacman -S --needed espeak-ng           # only for the test fixture
You do not need TensorRT installed. The ONNX Runtime TensorRT EP registers with bowdler but falls through to the CUDA EP if libnvinfer.so is missing.

Build
cargo build --release --workspace
Cold builds compile a lot (RocksDB, whisper.cpp, ffmpeg-sys-next, ort) — budget ~10 minutes the first time. Incremental builds are seconds.

Models
Both models are runtime downloads — they are not vendored in the repo. The easy path is one command:

bowdler models download                          # NudeNet + whisper-medium.en
bowdler models download --whisper-variant tiny.en
bowdler models download --no-audio               # vision only
bowdler models download --force                  # re-fetch even if present
Both models land in ~/.cache/bowdler/models/. Existing files are skipped unless --force is given (NudeNet is hash-checked against its pinned sha256). The subcommand needs uv and curl on $PATH.

NudeNet 320n — manual fallback
mkdir -p ~/.cache/bowdler/models
uv venv /tmp/nn && uv pip install --python /tmp/nn/bin/python nudenet
cp /tmp/nn/lib/python*/site-packages/nudenet/320n.onnx \
   ~/.cache/bowdler/models/nudenet-320n.onnx
rm -rf /tmp/nn
Pinned: nudenet 3.4.2’s bundled 320n.onnx, sha256 c15d8273adad2d0a92f014cc69ab2d6c311a06777a55545f2c4eb46f51911f0f.

Whisper — manual fallback
curl -L -o ~/.cache/bowdler/models/whisper-medium.en.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-medium.en.bin
For testing or low-VRAM systems, swap medium for tiny.

Your first cut
bowdler run movie.mkv -o movie.bowdler.mkv
That’s the one-shot path: detect → auto-approve high-confidence detections → render. If you’d rather review every detection by hand, use the three-step workflow on the CLI reference page.

-friendly cuts of the media you already own.

Personal use only. Not intended for redistribution or hosting as a service. · Source on GitHub
