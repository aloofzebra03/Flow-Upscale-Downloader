# Flow Upscale Downloader - Commands

These commands are for Windows PowerShell.

## Open the project folder

```powershell
cd "C:\Users\aryan\Desktop\Youtube Automation\Flow Upscale Downloader"
$Python = "C:\micromamba\envs\flow-downloader\python.exe"
$ProjectUrl = "https://labs.google/fx/tools/flow/project/b91ca69e-1ae6-4fa3-b405-09713711c331"
```

Using the Micromamba environment's Python directly avoids the harmless
`The system cannot find the path specified` message previously printed by
`micromamba run`.

## Create or update the Micromamba environment

Create it once:

```powershell
micromamba env create -f environment.yml -y
```

Update an existing environment:

```powershell
micromamba env update -n flow-downloader -f environment.yml -y
```

## Log in with the default automation profile

```powershell
& $Python flow_downloader.py --login-only --project-url $ProjectUrl
```

An ordinary Chrome window opens. Sign in manually, confirm that the Flow
project loads, close that Chrome window completely, and then press Enter in
PowerShell. Credentials are not saved in the script; Chrome stores the session
inside the local `.chrome-profile` directory.

## Log in to a different Google account (recommended method)

Give every Google account a separate profile, state manifest, download folder,
and log folder. Replace `google-account-2` with a short local label. It does not
need to be the email address.

```powershell
$Account = "google-account-2"
$ProfileDir = Join-Path $PWD ".chrome-profiles\$Account"
$Manifest = Join-Path $PWD ".flow-downloader-state-$Account.json"
$OutputDir = Join-Path $PWD "Flow Downloads\$Account"
$LogDir = Join-Path $PWD "logs\$Account"

& $Python flow_downloader.py --login-only `
  --project-url $ProjectUrl `
  --profile-dir $ProfileDir
```

In the Chrome window:

1. Sign in to the new Google account.
2. Verify the account can open the requested Flow project. If it cannot, share
   the project with that account or replace `$ProjectUrl` with its project URL.
3. Close every Chrome window belonging to this dedicated profile.
4. Return to PowerShell and press Enter.

Always use the same `$ProfileDir` when running downloads for that account:

```powershell
& $Python flow_downloader.py `
  --project-url $ProjectUrl `
  --queue queue.csv `
  --profile-dir $ProfileDir `
  --manifest $Manifest `
  --output-dir $OutputDir `
  --log-dir $LogDir
```

This prevents one account's login cookies, completed-item state, or downloaded
filenames from being confused with another account's data.

## Replace the account in the existing default profile

Using a separate profile is safer, but the existing `.chrome-profile` can be
switched without deleting it:

```powershell
& $Python flow_downloader.py --login-only --project-url $ProjectUrl
```

When Chrome opens, use the Google account menu to sign out, sign in to the new
account, verify Flow opens under the intended account, close Chrome, and press
Enter. Future commands that omit `--profile-dir` will use this newly signed-in
account. The existing default manifest and output folder are still shared, so
completed files may be skipped; separate account-specific paths avoid that.

## Check the names Flow exposes

Default profile:

```powershell
& $Python flow_downloader.py --project-url $ProjectUrl --list-names
```

Account-specific profile:

```powershell
& $Python flow_downloader.py `
  --project-url $ProjectUrl `
  --profile-dir $ProfileDir `
  --list-names
```

Queue processing uses Flow's search bar first and automatically retries a name
without its `.mp4` extension. Keep `.mp4` in `queue.csv`.

## Run the default queue

Default profile and default output/state paths:

```powershell
& $Python flow_downloader.py `
  --project-url $ProjectUrl `
  --queue queue.csv
```

The default download location is `Flow Downloads`. Press `Ctrl+C` to stop
safely. Running the same command again resumes from the manifest and skips
completed valid files.

The worker may show a background tab named `Flow downloader keepalive`. Leave it
open. If Flow/Chrome closes the work tab during an already-upscaled download, the
worker recovers the completed MP4 from its private staging directory and
automatically relaunches Chrome before continuing with the next queue item.

## Download images using the same queue.csv

Do not create a differently named queue. Replace the rows inside `queue.csv`
with image rows:

```csv
flow_name,output_filename
Image_Scene_01.png,scene_001.png
Image_Scene_02.png,scene_002.png
```

List the exact names Flow exposes in its Images view:

```powershell
& $Python flow_downloader.py `
  --media-type images `
  --project-url $ProjectUrl `
  --profile-dir $ProfileDir `
  --list-names
```

Download the image queue sequentially:

```powershell
& $Python flow_downloader.py `
  --ordinary-chrome `
  --media-type images `
  --project-url $ProjectUrl `
  --queue queue.csv `
  --profile-dir $ProfileDir `
  --manifest $Manifest `
  --output-dir $OutputDir `
  --log-dir $LogDir
```

`--ordinary-chrome` is recommended for image upscaling when the same profile
works manually but Flow rejects requests from Playwright's normal launch mode.
The worker starts visible ordinary Chrome, attaches through a localhost-only
debugging connection, and closes it at the end. Close all existing Chrome
windows using `$ProfileDir` before running this command.

Image mode supports `.png`, `.jpg`, `.jpeg`, and `.webp`. Flow sometimes
returns JPEG bytes for an image labeled PNG; the worker detects and converts
that file so the queue's exact output filename and extension remain valid. It selects
only the exact `2K Upscaled` image option and refuses to fall back to 1K, 4K,
or the parent Download action. Remove `--media-type images` to return to the existing 1080p
video workflow. The queue filename remains `queue.csv` in both modes.

## Run the tests

```powershell
& $Python -m unittest discover -s tests -v
```

## Important rules

- Do not keep the dedicated profile open in another Chrome process while the
  downloader runs.
- Do not run two workers with the same profile simultaneously.
- Close the login-only Chrome window before pressing Enter in PowerShell.
- CAPTCHA, reauthentication, subscription prompts, and account-access problems
  require manual attention; the worker does not bypass them.
- The worker selects only a menu item containing both `1080p` and `Upscaled`.
