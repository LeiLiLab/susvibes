#!/bin/bash

echo "Installing Real Gemini CLI..."

apt-get update
apt-get install -y curl

curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.2/install.sh | bash

source "$HOME/.nvm/nvm.sh"

nvm install 22
nvm use 22
npm -v

npm install -g @google/gemini-cli@latest

pip3 install "harness[sdk]"

echo "Setting up environment..."
mkdir -p /root/.gemini
cat > /root/.gemini/.env << 'EOF'
export GEMINI_MODEL="gemini-3-pro-preview"
export FORCE_AUTO_BACKGROUND_TASKS="1"
export ENABLE_BACKGROUND_TASKS="1"
EOF

echo "source /root/.gemini/.env" >> /root/.bashrc

echo "Testing installation..."
source /root/.gemini/.env
source "$HOME/.nvm/nvm.sh"
nvm use 22

if command -v gemini >/dev/null 2>&1; then
  gemini --version
  echo "INSTALL_SUCCESS"
else
  echo "Installation failed" >&2
  exit 1
fi