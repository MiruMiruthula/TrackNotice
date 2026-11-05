TRANSITNOTICE - READY TO USE  (needs Python 3.10+ and internet)

WINDOWS
  1. Unzip the folder anywhere (e.g. Documents).
  2. Open the inner "transitnotice" folder and DOUBLE-CLICK  START_WINDOWS.bat
  3. Wait. Your browser opens at http://127.0.0.1:5000
     The real Southern Railway notices load by themselves in 1-2 minutes. Click the "Archive" tab to see them
     (they are 2023-2025 notices, so "Active now" is empty until you add the live feed - see README.md).

MAC / LINUX:  run  ./start_mac_linux.sh

ADD REAL NOTICES (the official website does not allow automatic downloads, so you copy them - takes 1 minute per notice)
  1. Open http://127.0.0.1:5000/admin  and sign in (token = admin123).
  2. Click the link "official Southern Railway press-release page" in the Admin page, pick "Chennai" under Press Release - Divisions,
     open a notice (e.g. "Changes in the pattern of Train Services").
  3. Select all text (Ctrl+A), copy (Ctrl+C), go back to the Admin tab, paste into "Notice text", paste the notice's web address into
     the first box, click "Import this notice".
  4. Open the home page: the notice is under Active now / Upcoming / Archive depending on its dates.
  One real sample notice is already included (Archive tab).

CHECK STATUS
  * Home page top: LIVE / STALE / SOURCE ERROR labels.   * http://127.0.0.1:5000/sources : status, errors, refresh history.
  * Admin: http://127.0.0.1:5000/admin  (token = ADMIN_TOKEN in the .env file, default  admin123)  -> "Refresh now" button.

STOP: close the black window (or press Ctrl+C).   AGAIN LATER: double-click START_WINDOWS.bat again.
If you see SOURCE ERROR, open /sources and read the reason (robots_disallowed = the site does not allow automated downloads).
