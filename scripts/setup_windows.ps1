# CampusPlayer Windows Setup & Execution PowerShell Script
# Requires: Python 3.10+, PostgreSQL 15+, Redis, FFmpeg

Write-Host "=== CampusPlayer Windows Setup & Execution ===" -ForegroundColor Cyan

# 1. Environment file check
if (-not (Test-Path ".env")) {
    Write-Host "Creating .env from .env.example..." -ForegroundColor Yellow
    Copy-Item ".env.example" ".env"
}

# 2. Verify PostgreSQL Database URL
$env:DATABASE_URL = [System.Environment]::GetEnvironmentVariable("DATABASE_URL", "Process")
if (-not $env:DATABASE_URL) {
    $env:DATABASE_URL = "postgresql://postgres:postgres@localhost:5432/campusplayer"
}

Write-Host "Using Database URL: $env:DATABASE_URL" -ForegroundColor Green

# 3. Apply Migrations & Initialize Database
Write-Host "Initializing Database & Migrations..." -ForegroundColor Yellow
python scripts/init_db.py

# 4. Verify FFmpeg Availability
$ffmpeg = Get-Command ffmpeg -ErrorAction SilentlyContinue
if ($ffmpeg) {
    Write-Host "FFmpeg detected at: $($ffmpeg.Source)" -ForegroundColor Green
} else {
    Write-Host "WARNING: FFmpeg binary not found on PATH. Please set FFMPEG_PATH in .env" -ForegroundColor Red
}

Write-Host "`nTo start web application:" -ForegroundColor Cyan
Write-Host "  python app.py" -ForegroundColor White

Write-Host "`nTo start Celery worker:" -ForegroundColor Cyan
Write-Host "  celery -A celery_tasks.celery worker --loglevel=info --pool=solo" -ForegroundColor White
