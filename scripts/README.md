# scripts

Every routine command lives here so none of it survives only in shell
history. Run them from the repository root.

| Script | What it does |
|---|---|
| `install.sh` | **One-command setup for a fresh machine** — installs uv, ffmpeg, Ollama and the vision model, then prints the plugin + worker next steps. Start here. |
| `vlm-hosts.sh` | Manage the pool of Ollama servers the visual pass fans across (multi-GPU). `add`/`remove`/`set`/`list`/`clear`. |
| `setup.sh` | Install Python deps, re-apply the pureframe patches, check for ffmpeg/tesseract/ollama |
| `test.sh` | Run the Python test suite and build the plugin |
| `worker.sh` | Start the worker API on :8765 |
| `analyze.sh <paths...>` | Batch-analyze films (audio + visual) |
| `analyze-audio.sh <paths...>` | Profanity only — about a minute per film |
| `review.sh <film.mkv>` | Print the review URL for a film |
| `render.sh <film.mkv>` | Render a clean copy from approved findings |
| `build-plugin.sh [installDir]` | Build and package the Jellyfin plugin |

`.sh` files work in Git Bash on Windows as well as Linux. On Windows, install
Git Bash once (<https://git-scm.com/download/win>, or `winget install Git.Git`),
then run these with `bash scripts/<name>.sh`.
