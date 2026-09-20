# Google Flow Sequential Video and Image Downloader

This Windows tool processes named Google Flow media one item at a time. Video
mode requests the 1080p upscaled download. Image mode requests the 2K upscaled
download. Both modes validate the finished
file before moving to the next CSV row.

It does not use screen coordinates and does not store your Google password.

See [COMMANDS.md](COMMANDS.md) for copy-and-paste PowerShell commands, including
how to keep separate Google accounts in separate automation profiles.

## Setup

From PowerShell in this folder:

```powershell
micromamba env create -f environment.yml -y
micromamba run -n flow-downloader python flow_downloader.py --login-only
```

If PowerShell still begins with `(.venv)` from the deleted virtual environment,
run `deactivate` first or open a new PowerShell window. Otherwise Micromamba may
print a harmless `The system cannot find the path specified` warning.

If the environment already exists, update it with:

```powershell
micromamba env update -n flow-downloader -f environment.yml -y
```

An ordinary Chrome window opens with a dedicated profile stored in
`.chrome-profile`. It is intentionally launched without Playwright so Google
does not reject the login as an automated browser. Sign in manually, verify the
Flow project opens, **close that Chrome window**, then return to PowerShell and
press Enter. Do not open that dedicated profile in another Chrome process while
the downloader is running.

## Queue

To have the tool hover-scan Flow and print the exact video names it can see:

```powershell
micromamba run -n flow-downloader python flow_downloader.py --list-names
```

Edit `queue.csv` before a run:

```csv
flow_name,output_filename
Video_Scene_09.mp4,scene_009_1080p.mp4
Video_Scene_10.mp4,scene_010_1080p.mp4
```

- `flow_name` must exactly match the unique name displayed on the Flow card.
- `output_filename` must be a filename, not a path, and must end in `.mp4`.
- Names and output filenames cannot be duplicated.

The same `queue.csv` filename is also used for images. Replace its rows with
image names and use `.png`, `.jpg`, `.jpeg`, or `.webp` output filenames:

```csv
flow_name,output_filename
Image_Scene_01.png,scene_001.png
Image_Scene_02.png,scene_002.png
```

The output extension must match the format Flow downloads. PNG is the safest
choice when the Flow source name is a PNG.

For each queued clip, the worker uses Flow's top media search first. It tries
the CSV name as written and then retries without the `.mp4` suffix because
Flow's current search index exposes names such as `Video_Scene_02`. The result
must still display that exact base name before the worker accepts the card.
Deterministic scrolling remains available as a fallback if the search control
is unavailable.

## Run

```powershell
micromamba run -n flow-downloader python flow_downloader.py `
  --project-url "https://labs.google/fx/tools/flow/project/b91ca69e-1ae6-4fa3-b405-09713711c331" `
  --queue queue.csv
```

Completed media is saved in `Flow Downloads`. Progress is stored atomically
in `.flow-downloader-state.json`, so rerunning the command skips valid completed
files. Logs and failure screenshots are stored under `logs`.

The inactivity timeout defaults to five minutes and each item gets two retries.
While Flow visibly says `Upscaling your video`, the worker keeps waiting and
will not create a duplicate upscale job. Press `Ctrl+C` to stop safely.

The worker never overwrites an existing destination. Move or rename an existing
file before retrying that output name.

To list image names and then process image rows from the same `queue.csv`:

```powershell
micromamba run -n flow-downloader python flow_downloader.py `
  --media-type images `
  --project-url "https://labs.google/fx/tools/flow/project/b91ca69e-1ae6-4fa3-b405-09713711c331" `
  --list-names

micromamba run -n flow-downloader python flow_downloader.py `
  --media-type images `
  --project-url "https://labs.google/fx/tools/flow/project/b91ca69e-1ae6-4fa3-b405-09713711c331" `
  --queue queue.csv
```

Image mode requires the same submenu structure shown in Flow and selects only
an option containing both `2K` and `Upscaled`. It refuses to click 1K Original,
4K, or the parent Download row as a fallback.

## Troubleshooting

- If selectors no longer match after a Flow UI update, check the diagnostic
  screenshot in `logs/diagnostics`.
- If Google asks for authentication or reports that the browser may not be
  secure, close the automated Chrome window and rerun `--login-only`. Sign in in
  the ordinary Chrome window it opens, close it, and restart the queue.
- Close other windows using `.chrome-profile` before starting the tool.
- Use `--verbose` for more detailed logs.

## Tests

```powershell
micromamba run -n flow-downloader python -m unittest discover -s tests -v
```
