$ErrorActionPreference = 'Stop'

$app = New-Object -ComObject AcroExch.App
try {
    'Acrobat COM OK'
} finally {
    if ($app) {
        $app.Exit()
    }
}
