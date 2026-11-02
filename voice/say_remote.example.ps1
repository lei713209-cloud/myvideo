# 远端(Windows)批量 TTS 模板：SSH 进来后整段贴给 powershell -EncodedCommand 执行。
# 把 TEXTS 换成你的旁白数组；路径按你的运行时改。要点：显式 python.exe、UTF-8 环境、逐句落盘。
$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$py   = 'C:\path\to\tts-runtime\.venv\Scripts\python.exe'   # 你的 TTS venv
$say  = 'C:\path\to\say.py'
$outdir = 'D:\tts_out'
New-Item -ItemType Directory -Force -Path $outdir | Out-Null

$TEXTS = @(
  '第一句旁白文本。',
  '第二句旁白文本。'
)
$i = 0
foreach ($t in $TEXTS) {
  $i++
  $wav = Join-Path $outdir ("line{0:d2}.wav" -f $i)
  if (Test-Path $wav) { Write-Host "SKIP $wav"; continue }
  & $py $say --text $t --out $wav --voice default
  Write-Host "OK $wav"
}
Write-Host "ALL DONE"
