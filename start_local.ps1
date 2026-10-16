$ErrorActionPreference = 'Stop'

$taskPythonPath = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'

if (-not (Test-Path -LiteralPath $taskPythonPath)) {
    throw 'Create .venv and install requirements.txt first.'
}

& $taskPythonPath -m uvicorn app.api_run:app `
    --app-dir $PSScriptRoot `
    --host 127.0.0.1 `
    --port 8000

exit $LASTEXITCODE