### Install BloodHound CE

**Private (localhost only – 127.0.0.1):**

```bash
curl -fsSL https://raw.githubusercontent.com/mverschu/bhce/refs/heads/main/install.sh | sudo bash
```

**Public (all interfaces – 0.0.0.0):**

```bash
curl -fsSL https://raw.githubusercontent.com/mverschu/bhce/refs/heads/main/install.sh | sudo bash -s -- --public
```
