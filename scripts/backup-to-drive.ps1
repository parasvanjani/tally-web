<#
  Push finished TallyPrime backups to Google Drive, encrypted, with retention.

  Why this design: TallyPrime's live data folder must NOT live in a Google Drive
  sync folder (no database locking; half-synced files and corruption propagate).
  Instead TallyPrime writes a consistent backup (Backup Scheduler / Alt+Y > Backup)
  to a local folder, and this script copies only completed backup files to Drive
  through an rclone "crypt" remote, so Google only ever stores ciphertext.

  One-time rclone setup (see README step 6):
    rclone config  -> new remote "gdrive"     type: drive, scope: drive.file
    rclone config  -> new remote "tallycrypt" type: crypt, remote: gdrive:TallyBackups,
                      filename_encryption: standard, set a strong password + salt
    Store both crypt passwords in a password manager. Lose them = lose the backups.

  Schedule (daily 23:30, after Tally's own scheduled backup):
    schtasks /Create /TN "TallyDriveBackup" /SC DAILY /ST 23:30 /RU SYSTEM `
      /TR "powershell -NoProfile -ExecutionPolicy Bypass -File C:\TallyWeb\scripts\backup-to-drive.ps1"
#>
param(
  [string]$BackupDir   = "C:\TallyBackups",           # TallyPrime backup destination folder
  [string]$Remote      = "tallycrypt:",               # rclone crypt remote
  [int]   $KeepDays    = 90,                          # remote retention
  [int]   $SettleMins  = 15,                          # skip files still being written
  [string]$Rclone      = "C:\TallyWeb\rclone\rclone.exe",
  [string]$LogFile     = "C:\TallyWeb\logs\drive-backup.log"
)
$ErrorActionPreference = "Stop"
$env:RCLONE_CONFIG = "C:\TallyWeb\rclone\rclone.conf"
function Log($m) { "$(Get-Date -Format s)  $m" | Tee-Object -FilePath $LogFile -Append }

if (-not (Test-Path $BackupDir)) { Log "ERROR backup folder $BackupDir not found"; exit 2 }
$cutoff = (Get-Date).AddMinutes(-$SettleMins)
$ready = Get-ChildItem $BackupDir -Recurse -File | Where-Object { $_.LastWriteTime -lt $cutoff }
if (-not $ready) { Log "WARN no finished backup files in $BackupDir — is Tally's Backup Scheduler running?"; exit 3 }

$host_ = $env:COMPUTERNAME
Log "Uploading $($ready.Count) file(s) from $BackupDir to $Remote$host_"
# copy (never sync) so a local deletion or ransomware wipe can't erase the cloud copies
& $Rclone copy $BackupDir "$Remote$host_" --min-age "$($SettleMins)m" --transfers 2 `
  --checksum --log-file $LogFile --log-level INFO
if ($LASTEXITCODE -ne 0) { Log "ERROR rclone copy failed ($LASTEXITCODE)"; exit 4 }

& $Rclone check $BackupDir "$Remote$host_" --one-way --min-age "$($SettleMins)m" --log-file $LogFile
if ($LASTEXITCODE -ne 0) { Log "ERROR verification mismatch after upload"; exit 5 }

& $Rclone delete "$Remote$host_" --min-age "$($KeepDays)d" --log-file $LogFile
Log "OK backup uploaded and verified; remote copies older than $KeepDays days pruned"
