#!/bin/bash
KEY="/d/maczhuji"
HOST="47.120.39.220"
PASS="lengyan2"

eval "$(ssh-agent -s)" 2>/dev/null
ssh-add "$KEY" 2>/dev/null <<< "$PASS"

ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 -o BatchMode=yes "root@$HOST" '
echo "=== Nginx cert path ==="
CERT_FILE="/www/server/panel/vhost/cert/api.51ins.com/fullchain.pem"
echo "File: $CERT_FILE"
ls -la "$CERT_FILE"
echo ""
echo "=== Number of certs in fullchain ==="
grep -c "BEGIN CERTIFICATE" "$CERT_FILE"
echo ""
echo "=== Cert subjects in fullchain ==="
openssl crl2pkcs7 -nocrl -certfile "$CERT_FILE" 2>/dev/null | openssl pkcs7 -print_certs -noout 2>/dev/null
echo ""
echo "=== Let Encrypt cert path ==="
LE_CERT="/etc/letsencrypt/live/api.51ins.com/fullchain.pem"
ls -la "$LE_CERT" 2>/dev/null
echo "Certs in LE fullchain:"
grep -c "BEGIN CERTIFICATE" "$LE_CERT" 2>/dev/null
echo ""
echo "=== Are they the same file? ==="
diff "$CERT_FILE" "$LE_CERT" 2>/dev/null && echo "SAME" || echo "DIFFERENT"
echo ""
echo "=== External SSL test ==="
echo | openssl s_client -connect api.51ins.com:443 -servername api.51ins.com 2>/dev/null | grep -A2 "Certificate chain"
echo ""
echo "=== Verify depth ==="
echo | openssl s_client -connect 127.0.0.1:443 -servername api.51ins.com 2>&1 | grep -E "verify|depth|error"
echo ""
echo "=== certbot version ==="
certbot --version 2>/dev/null || echo "no certbot"
echo ""
echo "=== Python3 check certifi ==="
python3 -c "import certifi; print(certifi.__version__, certifi.where())" 2>/dev/null || echo "no python3 certifi"
echo ""
echo "=== DONE ==="
' 2>&1

eval "$(ssh-agent -k)" 2>/dev/null
