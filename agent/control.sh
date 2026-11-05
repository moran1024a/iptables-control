#!/usr/bin/env bash
# Authenticated control channel shared by the installation manager.
hex2bin() { printf "$(sed 's/../\\x&/g' <<< "$1")"; }
bin2hex() { od -An -v -tx1 | tr -d ' \n'; }
sha256_hex() { openssl dgst -sha256 | awk '{ print $NF }'; }

# HMAC-SHA256（RFC 2104）：hmac_hex 十六进制密钥 < 数据
hmac_hex() {
  local k="$1" i b ipad="" opad="" inner
  while [ "${#k}" -lt 128 ]; do k+="00"; done
  for ((i = 0; i < 128; i += 2)); do
    b=$((16#${k:i:2}))
    printf -v ipad '%s%02x' "$ipad" $((b ^ 0x36))
    printf -v opad '%s%02x' "$opad" $((b ^ 0x5c))
  done
  inner="$({ hex2bin "$ipad"; cat; } | sha256_hex)"
  { hex2bin "$opad"; hex2bin "$inner"; } | sha256_hex
}

EK="" MK=""
derive_keys() {
  EK="$(printf 'ictl-enc' | hmac_hex "$KEY")"
  MK="$(printf 'ictl-mac' | hmac_hex "$KEY")"
}

aes() {
  ICTL_EK="$EK" openssl enc -aes-256-ctr -pbkdf2 -iter 1 -md sha256 -pass env:ICTL_EK "$@" 2>/dev/null
}


send_request() {
  local out="$1" d code req_tag size
  d="$(mktemp -d /run/ictl.XXXXXX)" || { echo 000; return; }
  aes | tail -c +9 > "$d/msg"
  req_tag="$(hmac_hex "$MK" < "$d/msg")"
  { cat "$d/msg"; hex2bin "$req_tag"; } | openssl base64 -A > "$d/req"
  code="$(curl -sS --connect-timeout 5 --max-time "${2:-15}" -o "$d/body" -w '%{http_code}' \
    -H 'Content-Type: text/plain' --data-binary @"$d/req" "$URL" 2>/dev/null)"
  if [ "$code" = "200" ]; then
    # 校验响应的 HMAC（绑定本次请求）后解密
    code="bad"
    openssl base64 -d -A < "$d/body" > "$d/resp" 2>/dev/null
    size="$(wc -c < "$d/resp")"
    if [ "$size" -ge 40 ]; then
      head -c $((size - 32)) "$d/resp" > "$d/rmsg"
      if [ "$({ hex2bin "$req_tag"; cat "$d/rmsg"; } | hmac_hex "$MK")" = "$(tail -c 32 "$d/resp" | bin2hex)" ]; then
        { printf 'Salted__'; cat "$d/rmsg"; } | aes -d > "$out" && [ -s "$out" ] && code="200"
      fi
    fi
  fi
  rm -rf "$d"
  echo "${code:-000}"
}
