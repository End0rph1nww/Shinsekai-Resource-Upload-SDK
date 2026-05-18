# Copy this file to .online_env.ps1 and fill in your own test endpoint values.
# Do not commit .online_env.ps1.

$env:SHINSEKAI_ONLINE_TEST = "1"

# Required for live online tests.
$env:SHINSEKAI_BASE_URL = "https://api.example.com"
$env:SHINSEKAI_WEB_URL = "https://shinsekai.example.com"

# Optional: only needed for API-key upload smoke tests.
$env:SHINSEKAI_API_KEY = "sk-sn-your_key"

# Optional: set to "1" only when you really want a >20MB multipart upload test.
$env:SHINSEKAI_ONLINE_LARGE = ""

# Optional and destructive: creates users, claims, API keys, and test resources.
$env:SHINSEKAI_ONLINE_FULL = ""
