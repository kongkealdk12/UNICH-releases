# U'NICH — AI Video Translator

កម្មវិធីបកប្រែវីដេអូជាសំឡេងខ្មែរ (Windows)។
This repository is the **public download and auto-update channel** for U'NICH.
It holds installers only — no source code.

## ⬇ ទាញយក / Download

**[Download the latest version](https://github.com/kongkealdk12/UNICH-releases/releases/latest)**

ជ្រើសឯកសារ `UNICH_Setup_vX.Y.Z.exe` នៅក្រោម **Assets** នៃ release ថ្មីបំផុត។
Pick `UNICH_Setup_vX.Y.Z.exe` under **Assets** in the newest release.

## ការតម្លើង / Installing

1. ដំណើរការ (run) `UNICH_Setup_vX.Y.Z.exe`។
2. Windows នឹងសួរសិទ្ធិ Administrator — ចុច **Yes** (កម្មវិធីតម្លើងទៅ `C:\Program Files\UNICH`)។
3. បើ Windows SmartScreen ដាស់តឿន៖ **More info → Run anyway**។ កម្មវិធីតម្លើងមិនមាន code-signing certificate ទេ។

## ការធ្វើបច្ចុប្បន្នភាព / Updating

កម្មវិធីពិនិត្យរកជំនាន់ថ្មីនៅទីនេះដោយស្វ័យប្រវត្តិពេលបើក ហើយអ្នកអាចចុចប៊ូតុង `⟳`
នៅរបារខាងក្រោមដើម្បីពិនិត្យដោយដៃ។ វានឹងទាញយក និងតម្លើងជំនាន់ថ្មីជំនួសឲ្យអ្នក។

The app checks this repository on startup and via the `⟳` button in the status
bar, then downloads and runs the new installer for you.

## ការផ្ទៀងផ្ទាត់ការទាញយក / Verifying a download

Release រាល់មួយមានឯកសារ `SHA256SUMS.txt`។ ដើម្បីផ្ទៀងផ្ទាត់នៅក្នុង PowerShell៖

```powershell
Get-FileHash .\UNICH_Setup_vX.Y.Z.exe -Algorithm SHA256
```

តម្លៃដែលបានត្រូវផ្គូផ្គងនឹងតម្លៃក្នុង `SHA256SUMS.txt`។ កម្មវិធីធ្វើការផ្ទៀងផ្ទាត់នេះ
ដោយស្វ័យប្រវត្តិពេលធ្វើបច្ចុប្បន្នភាព ហើយបដិសេធឯកសារដែលមិនត្រូវគ្នា។

The in-app updater performs this check automatically and refuses a download
whose hash does not match.

## តម្រូវការប្រព័ន្ធ / Requirements

- Windows 10 or 11, 64-bit
- ~2 GB free disk space
- អ៊ីនធឺណិត (សម្រាប់ AI model លើកដំបូង, ការបកប្រែ និងសំឡេង TTS)
- NVIDIA GPU (ស្រេចចិត្ត) — បង្កើនល្បឿនប្រតិចារិក និងការដកសំឡេងដើម

## ជំនួយ / Support

បញ្ហា ឬសំណួរ៖ បើក [issue](https://github.com/kongkealdk12/UNICH-releases/issues)។

---

Source code is kept in a private repository. Only build outputs are published here.
