# scripts

Every routine command lives here so none of it survives only in shell
history. Run them from the repository root.

| Script | What it does |
|---|---|
| `setup.sh` | Install Python deps, re-apply the pureframe patches, check for ffmpeg/tesseract/ollama |
| `test.sh` | Run the Python test suite and build the plugin |
| `worker.sh` | Start the worker API on :8765 |
| `analyze.sh <paths...>` | Batch-analyze films (audio + visual) |
| `analyze-audio.sh <paths...>` | Profanity only — about a minute per film |
| `review.sh <film.mkv>` | Print the review URL for a film |
| `render.sh <film.mkv>` | Render a clean copy from approved findings |
| `build-plugin.sh [installDir]` | Build and package the Jellyfin plugin |

`.sh` files work in Git Bash on Windows as well as Linux.
