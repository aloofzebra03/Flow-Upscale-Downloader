# Faceless German image download — default profile selected; started 2026-10-06

Verified queue.csv contains the full 19-image German_AF library: nine AI01–AI09 photographs, six GFX01–GFX06 graphics and four SRC01–SRC04 source-summary cards, in category/identifier order. Generation and naming are verified; factual/text approval of all ten graphics/cards is pending. No download command executed.

User confirmed Aryan ChatGPT Flow account uses `.chrome-profile` (default), NOT google-account-2/3/4. This does not establish other account mappings. Flow URL `/u/1` is independent of the local profile label. Selected current-run output folder was created and verified empty; no old files needed moving. Existing unrelated google-account-3/4 download folders were untouched.

## Prepared PowerShell command (do not run until account confirmed)

```powershell
Set-Location -LiteralPath 'C:\Users\aryan\Desktop\Youtube Automation\Flow Upscale Downloader'
$Python = 'C:\micromamba\envs\flow-downloader\python.exe'
$ProjectUrl = 'https://flow.google.com/u/1/project/b91ca69e-1ae6-4fa3-b405-09713711c331'

# Fill both values only after verifying the local profile/account correspondence.
$Account = 'default'
$ProfileDir = Join-Path $PWD '.chrome-profile'
if ([string]::IsNullOrWhiteSpace($Account) -or [string]::IsNullOrWhiteSpace($ProfileDir)) {
    throw 'STOP: Confirm and fill the Google account label and exact existing profile directory first.'
}
if (-not (Test-Path -LiteralPath $ProfileDir -PathType Container)) {
    throw 'STOP: Selected profile directory does not exist.'
}

$Manifest = Join-Path $PWD '.flow-downloader-state-default-German_AF_Aufschieben-images.json'
$OutputDir = Join-Path $PWD "Flow Downloads\$Account\German_AF_Aufschieben_Images"
$LogDir = Join-Path $PWD "logs\$Account\German_AF_Aufschieben_Images"

# Close other Chrome windows using this dedicated profile before starting.
# Never switch accounts, purchase credits or bypass authentication/security prompts automatically.
& $Python flow_downloader.py `
    --ordinary-chrome `
    --media-type images `
    --project-url $ProjectUrl `
    --queue queue.csv `
    --profile-dir $ProfileDir `
    --manifest $Manifest `
    --output-dir $OutputDir `
    --log-dir $LogDir `
    --max-retries 0
```

Command adapted from COMMANDS.md's existing image-download section. Existing worker selects 2K Upscaled for images. Command started 2026-10-06: ordinary Chrome attached successfully, AI01 found, 2K Upscaled requested and Flow reported active upscaling. Completion NOT yet established. No downloader code or login/profile files modified. Automatic per-item retries disabled for this first run. Before a NEW run empty the exact destination by recoverably archiving prior items into images_old with collision-safe names; on a SAME-run resume retain valid current outputs/state. Verify all 19 files at completion before moving/copying into media folders or importing into Resolve; those steps need separate authorization.
