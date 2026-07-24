# Putting Simple Budget in the cloud (phone access with your PC off)

Goal: run `server.py` on PythonAnywhere's free tier so your phone reaches the
app at `https://YOURUSERNAME.pythonanywhere.com` even when your computer is
off. The bank scraper stays on your PC (it has to drive a real browser through
your bank's login) and pushes each import up to the cloud.

Your secret access key was generated separately and given to you in chat —
it is deliberately NOT written in this file, because this file lives in the
public GitHub repo. Keep the key private; below it's shown as `YOUR-KEY`.

Everything behind `/api/` requires this key, so your financial data is not
readable by anyone who merely finds the URL.

---

## 1. Create the account  (only you can do this)

1. Go to https://www.pythonanywhere.com/registration/register/beginner/
2. Sign up for the free **Beginner** plan (no credit card). Verify your email.
3. Pick a username — your app URL becomes `https://<username>.pythonanywhere.com`.
   Tell me the username and I'll fill in `cloud_config.json` for the scraper.

## 2. Get the code onto PythonAnywhere

Open a **Bash console** (Dashboard → "New console" → Bash) and run:

    git clone https://github.com/fmuegge25/Budget-app.git
    cd Budget-app
    pip install --user -r requirements.txt

(Your repo must have the latest code pushed first — see the note at the bottom.)

## 3. Add the secret key on the server

In that same Bash console, create the key file the server reads:

    echo "YOUR-KEY" > server_key.txt

## 4. Upload your real budget data

Your transaction data is NOT in the git repo (it's private). Upload it:

- Dashboard → **Files** → navigate into `Budget-app` → **Upload a file** →
  choose your local `C:\Users\fmueg\Documents\simple-budget\budget_data.json`.

## 5. Create the web app

1. Dashboard → **Web** tab → **Add a new web app**.
2. Choose **Manual configuration** (NOT the "Flask" quickstart) → pick the
   Python 3.x version.
3. When it's created, scroll to **Code** → click the **WSGI configuration
   file** link. Delete everything in it and replace with:

       import sys
       path = '/home/YOURUSERNAME/Budget-app'
       if path not in sys.path:
           sys.path.insert(0, path)
       from server import app as application

   (Replace `YOURUSERNAME`.) Save.
4. Still on the Web tab, set **Source code** to `/home/YOURUSERNAME/Budget-app`.
5. Click the big green **Reload** button.

## 6. Test it

- On your phone, open: `https://YOURUSERNAME.pythonanywhere.com/?key=YOUR-KEY`
  The key is saved to that device and stripped from the address bar, so you
  only paste it the first time. Then "Add to Home Screen" for the app icon.
- Do the same one-time `?key=...` visit on your laptop browser.

## 7. Point the bank scraper at the cloud

Back on your PC, I'll create `cloud_config.json` (from
`cloud_config.example.json`) with your real URL + the key. After that, the
daily/`Run Bank Scraper.bat` scraper posts imports straight to the cloud, so
your phone sees new transactions without the PC needing to serve anything.

---

## Notes / gotchas

- **Free tier renewal:** PythonAnywhere disables free web apps after 3 months
  unless you click a "Run until 3 months from today" button on the Web tab.
  Just click it whenever you log in; your data isn't lost, the app just needs
  the click to keep serving.
- **Your old local setup still works** unchanged — the local server has no
  `server_key.txt`, so it stays keyless on your tailnet. The cloud copy is the
  one with the key.
- **Updating the app later:** push code changes to GitHub, then in the
  PythonAnywhere Bash console `cd Budget-app && git pull`, and click Reload.
- **The repo must be current first.** The newest features (Possible Duplicates
  review, this cloud/auth support) are uncommitted locally right now — they
  have to be committed and pushed before `git clone`/`git pull` will bring the
  real current app to the cloud.
