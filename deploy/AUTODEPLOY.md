# Automatic deploys

Every push to `main` deploys to budget.dibberlab.me through
`.github/workflows/deploy.yml`. GitHub SSHes into the droplet with its own key,
and that key can only run `/usr/local/bin/budget-deploy` (pull `main`,
reinstall packages if `requirements.txt` changed, restart `budget`, check it
came back up). A failed deploy shows as a red X on the commit in GitHub, with
the server logs in the run output.

You can also redeploy by hand: GitHub → Actions → Deploy → Run workflow.

## One-time setup

Run these on your Mac.

1. Make a key just for GitHub deploys (no passphrase):

       ssh-keygen -t ed25519 -N "" -C "github-actions-budget-deploy" -f ~/.ssh/budget-gh-deploy

2. Install the deploy script on the droplet (after this branch is merged into
   `main`):

       ssh dibberlab-droplet 'cd /var/www/budget && git pull --ff-only origin main && install -m 755 deploy/budget-deploy.sh /usr/local/bin/budget-deploy'

3. Authorize the key on the droplet, locked to that script:

       ssh dibberlab-droplet "echo 'command=\"/usr/local/bin/budget-deploy\",no-port-forwarding,no-agent-forwarding,no-X11-forwarding,no-pty $(cat ~/.ssh/budget-gh-deploy.pub)' >> /root/.ssh/authorized_keys"

4. Add three repository secrets (GitHub → DibberLab/budget → Settings →
   Secrets and variables → Actions → New repository secret):

   | Name                 | Value                                              |
   |----------------------|----------------------------------------------------|
   | `DEPLOY_HOST`        | `209.97.148.9`                                     |
   | `DEPLOY_SSH_KEY`     | output of `cat ~/.ssh/budget-gh-deploy` (private)  |
   | `DEPLOY_KNOWN_HOSTS` | output of `ssh-keyscan -t ed25519 209.97.148.9`    |

   Or with the GitHub CLI:

       gh secret set DEPLOY_HOST -R DibberLab/budget -b 209.97.148.9
       gh secret set DEPLOY_SSH_KEY -R DibberLab/budget < ~/.ssh/budget-gh-deploy
       ssh-keyscan -t ed25519 209.97.148.9 | gh secret set DEPLOY_KNOWN_HOSTS -R DibberLab/budget

5. Test it: GitHub → Actions → Deploy → Run workflow.

After that you can delete the private key from your Mac
(`rm ~/.ssh/budget-gh-deploy`); GitHub has its copy.

## Notes

- If you change `deploy/budget-deploy.sh`, re-run the `install` line from step 2
  so the server uses the new version.
- To turn auto-deploy off, delete that key's line from `/root/.ssh/authorized_keys`.
- Deploys don't touch the database. Back it up yourself before any change that
  migrates data.
