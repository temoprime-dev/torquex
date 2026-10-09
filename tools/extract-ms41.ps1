param(
    [string]$ArchivePath = "E:\Tuning-Files_EUROTUN_11.16.rar",
    [string]$RepositoryPath = "$env:USERPROFILE\torquex_generator"
)

$ErrorActionPreference = "Stop"
$RepositoryUrl = "https://github.com/temoprime-dev/torquex_generator.git"
$Branch = "arena/01a10aff-torquex-generator"

function Find-SevenZip {
    $candidates = @(
        "$env:ProgramFiles\7-Zip\7z.exe",
        "${env:ProgramFiles(x86)}\7-Zip\7z.exe",
        "7z.exe"
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return $candidate
        }
        $command = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($null -ne $command) {
            return $command.Source
        }
    }
    throw "7-Zip was not found. Install it from https://www.7-zip.org/"
}

function Run-Git {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    & git @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Git command failed: git $($Arguments -join ' ')"
    }
}

$wantedFiles = @(
    @{
        Source = "BMW_E36_328i_1997_Benzin___142KWKW_Bosch_1429861_Siemens__4101_61AC.Original"
        Destination = "MS41_1429861_E36_328i_M52B28_1997_4101_partial.bin"
        Size = 24576
    },
    @{
        Source = "BMW_E39_528i_1996_Benzin___142KWKW_Bosch_1429861__4101_6701.Original"
        Destination = "MS41_1429861_E39_528i_M52B28_1996_4101_partial.bin"
        Size = 24576
    },
    @{
        Source = "BMW_E39_528i_1997_Benzin___142KWKW_Bosch_1429861_Siemens__4101_6BDC.Original"
        Destination = "MS41_1429861_E39_528i_M52B28_1997_4101_partial_A.bin"
        Size = 24576
    },
    @{
        Source = "BMW_E39_528i_1997_Benzin___142KWKW_Bosch_1429861__4101_691D.Original"
        Destination = "MS41_1429861_E39_528i_M52B28_1997_4101_partial_B.bin"
        Size = 24576
    },
    @{
        Source = "BMW_E36_320i_1996_Benzin___110.3KWKW_Bosch_1429861__4101_2567.Original"
        Destination = "MS41_1429861_E36_320i_M52B20_1996_4101_partial.bin"
        Size = 24576
    },
    @{
        Source = "BMW_E36_320i_1998_Benzin___110.3KWKW_Bosch_1429861__4101_3603.Original"
        Destination = "MS41_1429861_E36_320i_M52B20_1998_4101_partial.bin"
        Size = 24576
    },
    @{
        Source = "BMW_E39_523i_1998_Benzin___125KWKW_Bosch_1429861_Siemens__000115852_AC92.Original"
        Destination = "MS41_1429861_E39_523i_M52B25_1998_115852_partial.bin"
        Size = 24576
    },
    @{
        Source = "BMW_E39_528i_1995_Benzin___142KWKW_Siemens_1429861_5WK90322_0060041010000_642C.Original"
        Destination = "MS41_1429861_E39_528i_M52B28_1995_0060041010000_full.bin"
        Size = 262144
    },
    @{
        Source = "BMW_E36_320i__Benzin___110.3KWKW_Bosch_1429861_90322_0060041010000_216F.Original"
        Destination = "MS41_1429861_E36_320i_M52B20_0060041010000_full_A.bin"
        Size = 262144
    },
    @{
        Source = "BMW_E36_320i__Benzin___110.3KWKW_Bosch_1429861_90322_0060041010000_3406.Original"
        Destination = "MS41_1429861_E36_320i_M52B20_0060041010000_full_B.bin"
        Size = 262144
    },
    @{
        Source = "BMW_E39_520i_1997_Benzin___110KWKW_Siemens_5WK90322___Siemens_1429861_078F.Original"
        Destination = "MS41_1429861_E39_520i_M52B20_1997_full.bin"
        Size = 262144
    },
    @{
        Source = "BMW_E39_520i_1998_Benzin___110KWKW_Siemens_5KW90322_Siemens_1429861_0841.Original"
        Destination = "MS41_1429861_E39_520i_M52B20_1998_full.bin"
        Size = 262144
    },
    @{
        Source = "BMW_Z3_2.8i_2004____142KWKW_Siemens_1429861_Siemens_115852_A1CF.Original"
        Destination = "MS41_1429861_Z3_28i_M52B28_115852_full_unverified.bin"
        Size = 262144
    }
)

$sevenZip = Find-SevenZip
if (-not (Test-Path -LiteralPath $ArchivePath -PathType Leaf)) {
    throw "Archive was not found: $ArchivePath"
}
if ($null -eq (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "Git was not found in PATH."
}

$securePassword = Read-Host "Archive password" -AsSecureString
$passwordPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePassword)
try {
    $archivePassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($passwordPointer)
}
finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($passwordPointer)
}
if ([string]::IsNullOrWhiteSpace($archivePassword)) {
    throw "Archive password is empty."
}

if (-not (Test-Path -LiteralPath (Join-Path $RepositoryPath ".git"))) {
    Run-Git clone $RepositoryUrl $RepositoryPath
}

Push-Location $RepositoryPath
try {
    $dirty = & git status --porcelain
    if ($LASTEXITCODE -ne 0) { throw "git status failed." }
    if ($dirty) { throw "Repository has uncommitted changes. Commit or remove them first." }

    Run-Git fetch origin
    & git checkout $Branch
    if ($LASTEXITCODE -ne 0) {
        Run-Git checkout -b $Branch "origin/$Branch"
    }
    Run-Git pull --ff-only origin $Branch

    $outputPath = Join-Path $RepositoryPath "MS41\firmware"
    $checksumPath = Join-Path $RepositoryPath "MS41\checksums\FIRMWARE_SHA256SUMS.txt"
    New-Item -ItemType Directory -Force -Path $outputPath | Out-Null
    New-Item -ItemType Directory -Force -Path (Split-Path $checksumPath -Parent) | Out-Null

    Write-Host "Reading archive inventory..."
    $listing = & $sevenZip l "-p$archivePassword" -slt $ArchivePath
    if ($LASTEXITCODE -ne 0) { throw "Unable to read archive. Check path and password." }
    $archiveEntries = @(
        $listing |
            Where-Object { $_ -like "Path = *" } |
            ForEach-Object { $_.Substring(7).Trim() } |
            Where-Object { $_ -and ($_ -ne $ArchivePath) }
    )

    $tempRoot = Join-Path $env:TEMP ("torquex-ms41-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Force -Path $tempRoot | Out-Null
    $extractedCount = 0
    $missing = @()

    try {
        foreach ($wanted in $wantedFiles) {
            $matches = @(
                $archiveEntries | Where-Object {
                    $leaf = Split-Path ($_ -replace "/", "\") -Leaf
                    ($leaf -eq $wanted.Source) -or ($leaf -eq ($wanted.Source + ".bin"))
                }
            )
            if ($matches.Count -eq 0) {
                Write-Warning ("Not found: " + $wanted.Source)
                $missing += $wanted.Source
                continue
            }

            $entry = $matches[0]
            $tempDirectory = Join-Path $tempRoot ([guid]::NewGuid().ToString("N"))
            New-Item -ItemType Directory -Force -Path $tempDirectory | Out-Null
            Write-Host ("Extracting: " + $entry)
            & $sevenZip e "-p$archivePassword" -y "-o$tempDirectory" $ArchivePath $entry | Out-Host
            if ($LASTEXITCODE -ne 0) { throw "7-Zip extraction failed: $entry" }

            $resultFiles = @(Get-ChildItem -LiteralPath $tempDirectory -File -Recurse)
            if ($resultFiles.Count -ne 1) {
                throw "Unexpected extracted file count for $entry : $($resultFiles.Count)"
            }
            $resultFile = $resultFiles[0]
            $destinationName = $wanted.Destination
            if ($resultFile.Length -ne $wanted.Size) {
                Write-Warning "Unexpected size for $entry : $($resultFile.Length), expected $($wanted.Size)"
                $destinationName = "UNVERIFIED_SIZE_" + $destinationName
            }
            Copy-Item -LiteralPath $resultFile.FullName -Destination (Join-Path $outputPath $destinationName) -Force
            $extractedCount++
        }
    }
    finally {
        if (Test-Path -LiteralPath $tempRoot) {
            Remove-Item -LiteralPath $tempRoot -Recurse -Force
        }
        $archivePassword = $null
    }

    if ($extractedCount -eq 0) { throw "No files were extracted." }

    $checksumLines = @(
        Get-ChildItem -LiteralPath $outputPath -File -Filter "*.bin" |
            Sort-Object Name |
            ForEach-Object {
                $hash = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
                "$hash  firmware/$($_.Name)"
            }
    )
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [IO.File]::WriteAllLines($checksumPath, $checksumLines, $utf8NoBom)

    Write-Host "Extracted files: $extractedCount"
    Write-Host "Missing files: $($missing.Count)"
    Get-ChildItem -LiteralPath $outputPath -File | Select-Object Name, Length | Format-Table -AutoSize

    Run-Git add "MS41/firmware" "MS41/checksums/FIRMWARE_SHA256SUMS.txt"
    $pending = & git status --porcelain
    if (-not $pending) {
        Write-Host "No new Git changes."
        exit 0
    }
    Run-Git commit -m "Add original MS41 firmware dumps"
    Run-Git push origin $Branch
    Write-Host "Done. Pull request: https://github.com/temoprime-dev/torquex_generator/pull/1"
}
finally {
    Pop-Location
}
