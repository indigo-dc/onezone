#!/bin/bash


echo -e "\e[1;33m"
echo "-------------------------------------------------------------------------"
echo "Starting Onezone in demo mode..."
echo "When the service is ready, an adequate log will appear here."
echo "-------------------------------------------------------------------------"
echo -e "\e[0m"

HN=$(hostname)
export ONEPANEL_DEBUG_MODE="true" # prevents container exit on configuration error
export ONEPANEL_BATCH_MODE="true"
export ONEPANEL_LOG_LEVEL="info" # prints logs to stdout (possible values: none, debug, info, error), by default set to info
export ONEPANEL_EMERGENCY_PASSPHRASE="password"
export ONEPANEL_GENERATE_TEST_WEB_CERT="true"  # default: false
export ONEPANEL_GENERATED_CERT_DOMAIN="onezone.local"  # default: ""
export ONEPANEL_TRUST_TEST_CA="true"  # default: false

export ONEZONE_CONFIG=$(cat <<EOF
        cluster:
          domainName: "onezone.local"
          nodes:
            n1:
              hostname: "${HN}"
          managers:
            mainNode: "n1"
            nodes:
              - "n1"
          workers:
            nodes:
              - "n1"
          databases:
            nodes:
              - "n1"
        onezone:
          name: "${HN}"
          domainName: "onezone.local"
          letsEncryptEnabled: false
EOF
)

sed "s/${HN}\$/${HN}-node.onezone.local ${HN}-node/g" /etc/hosts > /tmp/hosts.new
cat /tmp/hosts.new > /etc/hosts
rm /tmp/hosts.new
echo "127.0.1.1 ${HN}.onezone.local ${HN}" >> /etc/hosts

cat << EOF > /etc/oz_worker/config.d/disable-gui-verification.config
[
    {oz_worker, [
        % verification is not relevant in demo deployments, and turning it off allows deploying
        % any Oneprovider version (especially not an official release) alongside Onezone
        {gui_package_verification, false}
    ]}
].
EOF

# Run an async process to await service readiness
{
    if ! await; then
        kill -9 "$(pgrep -f /root/onezone.py)"
        exit 1
    fi

    IP=$(hostname -I | tr -d ' ')
    echo -e "\e[1;32m"
    echo "-------------------------------------------------------------------------"
    echo "Onezone service is ready!"
    echo "Visit https://${IP} in your browser (ignore the untrusted cert)."
    echo "You may also add such an entry to /etc/hosts: \"${IP} onezone.local\""
    echo "and visit https://onezone.local, because for some browsers"
    echo "the UI may not function correctly when using the IP address."
    echo "-------------------------------------------------------------------------"
    echo -e "\e[0m"
} &
