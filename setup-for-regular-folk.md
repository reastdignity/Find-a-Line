# Find a Line: step-by-step guide
This guide takes you from nothing to your first map of possible highlines. You don't need to know programming; you will copy a few commands into a terminal window. Setting things up takes about 15 minutes and is done only once.

**What you need**
- A computer with Windows or macOS
- An internet connection (the elevation data and aerial photos are downloaded automatically)
- About 1 GB of free disk space for a few searches

The tool works best in Poland and Malta, where it uses very detailed national elevation data (1 m). It also works anywhere else in Europe with less detailed data (30 m), which is good enough to find long lines only. It picks the right data by itself.

## 1. Install the tools (once)
### Windows
1. Install Python from [python.org/downloads](https://www.python.org/downloads/). In the first window of the installer, tick **"Add python.exe to PATH"**, then click *Install Now*.
2. Install Git from [git-scm.com/downloads/win](https://git-scm.com/downloads/win). The default options are fine.
3. Open PowerShell: press the Windows key, type `powershell` and press Enter. A window with a blinking cursor opens; this is where you type the commands below (paste with right-click or Ctrl+V, then press Enter).

### macOS
1. Open the Terminal: press Command+Space, type `terminal` and press Enter.
2. Install Homebrew (a tool that installs other tools):
   `/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"`
   It asks for your computer's password; nothing appears on the screen while you type it, that's normal. At the end it may print two or three commands under "Next steps"; copy and run them too.
3. Install Git and Python: `brew install git python`

## 2. Download the project (once)
In PowerShell (Windows) or the Terminal (macOS):

```
git clone https://github.com/reastdignity/Find-a-Line.git
cd Find-a-Line
```

This creates a `Find-a-Line` folder in your user folder (for example `C:\Users\YourName\Find-a-Line` or `/Users/YourName/Find-a-Line`) and moves into it.

## 3. Install what the script needs (once)
Still in the same window, create a separate Python environment for the project and install the libraries into it.

**Windows (PowerShell):**
```
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
```
If you get an error saying that running scripts is disabled, run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, answer `Y`, and run `.\venv\Scripts\Activate.ps1` again.

**macOS (Terminal):**
```
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

When the environment is active, the line in the window starts with `(venv)`. The installation takes a few minutes.

## 4. Your first search
1. Pick the centre of the area you want to search. The easiest way is Google Maps: right-click a spot and click the coordinates at the top of the menu to copy them (for example `50.15595, 19.75627`, which is the Żabi Koń rock in Dolina Kobylańska near Kraków).
2. Start the search with those coordinates (latitude first, then longitude):

   ```
   python findaline.py --lat 50.15595 --lon 19.75627
   ```

   On macOS use `python3` if `python` is not found.

3. The script searches within 1 km of that point. You will see what it is doing, and two progress bars at the bottom of the window: **Download** for the data it fetches and **Compute** for the calculations, each with the time left.
4. The first search in a new place downloads the elevation data (about 100 MB) and aerial photos around the lines it finds, so it can take 10–20 minutes. Searching the same area again is much faster, because everything downloaded is kept in the `cache` folder.
5. If a search would take very long, the script tells you how long and asks `Continue? [y/N]`. Type `y` and press Enter to go on, or just Enter to stop.

When it is done, the coordinates of the spots it found are printed (one line per spot; similar lines are grouped together), and a simple map opens in a separate window. Close that window to finish.

## 5. Look at the results
Open the `Find-a-Line` folder (in File Explorer or Finder) and double-click **`findaline_map.html`**. It opens the map of your latest search in your web browser:

- **Pink lines** have enough clearance over the terrain and over the trees; **orange lines** clear the terrain but pass too close to trees or buildings.
- **Click a line** to see its length, the coordinates and heights of both anchors, and the clearance. Drag the box by its grey top bar if it covers the line.
- At the same time, a **terrain profile** of the line opens at the bottom: the ground, the trees, the safety limit and how much the webbing sags. Move the mouse over the profile to see the values at that point, where it is on the map, and how the webbing sags with someone standing there. You can change the tension, webbing and weight below the profile.
- In the **top-right corner** switch the background to **Orthophoto** to see aerial photos (you can zoom in very close to check the rocks and trees). The photos around the found lines also work without internet.
- In the **bottom-left corner** you can show every single line instead of one per spot, or change how lines are grouped.
- In the **top-left corner** you can switch to any of your earlier searches.

Every search is kept in the `runs` folder. Open `runs/index.html` for a list of all searches with links to their maps and results; the `lines.csv` file of a search opens in Excel or any spreadsheet program.

## 6. Change the search
Add options to the command to change what is searched. Some useful ones:

| Option | What it does | Default |
| --- | --- | --- |
| `--radius 2000` | Search area around the centre, in metres | 1000 |
| `--min-length 50 --max-length 300` | Shortest and longest line, in metres | 30 and 1000 |
| `--slope-threshold 60` | How steep the ground must be for an anchor (degrees); higher means only cliffs, fewer lines and a faster search | 45 |
| `--resolution 1` | Detail of the terrain in metres; 1 is the most detailed but slower | 2 |
| `--nmpt reject` | Leave out lines that pass too close to trees or buildings; `--nmpt off` doesn't check trees at all (faster, less to download) | lines are marked in orange |
| `--model sag` | Check the clearance with the webbing sag calculation instead of the simple formula; set your setup with `--pretension 3` (kN), `--stretch 4` (% at 10 kN), `--webbing-weight 60` (g/m), `--athlete-mass 75` (kg), `--fall-zone 4` (m) | simple formula |
| `--name "My crag"` | Name of the search in the list | name of the place from OpenStreetMap |

For example, only longer lines over cliffs, within 2 km:

```
python findaline.py --lat 50.15595 --lon 19.75627 --radius 2000 --min-length 100 --slope-threshold 60
```

To see all options, run `python findaline.py --help`.

## Outside Poland
- **Malta** works the same way as Poland, with detailed data and aerial photos.
- **Lines over the sea** (between sea cliffs) are found too. Their box on the map says how many metres are over water, and the profile shows the water in blue.
- **Elsewhere** the script uses Europe-wide data with a 30 m grid. It finds long lines (roughly 100 m and more) reliably, short ones not. These data are free for non-commercial use only. Aerial photos are shown online (choose *Esri World Imagery* in the top-right corner).
- If you have your own elevation file (for example NASADEM from [OpenTopography](https://portal.opentopography.org)), use it with `--elevation-file path/to/file.tif`.
- To download NASADEM automatically (`--source nasadem`), you need a free OpenTopography API key, see below.

### Getting an OpenTopography API key (optional)
Only needed for `--source nasadem`.

1. Go to [portal.opentopography.org](https://portal.opentopography.org) and click **Register** in the top-right corner. Fill in your e-mail, name and password, and say briefly what you use the data for (for example "personal, recreation"). Confirm your e-mail address with the link you receive.
2. Log in and open **[MyOpenTopo](https://portal.opentopography.org/myopentopo)** (your account page).
3. Click **Request API Key**. The key, a long string of letters and digits, appears on the page straight away. Copy it.
4. Save it on your computer, so you don't have to type it every time:
   - **Windows (PowerShell):** `setx OPENTOPOGRAPHY_API_KEY "YOUR_KEY"`
   - **macOS (Terminal):** `echo 'export OPENTOPOGRAPHY_API_KEY="YOUR_KEY"' >> ~/.zshrc`

   Then close the window and open a new one; the script finds the key by itself. (Instead you can add `--opentopo-key YOUR_KEY` to each command.)
5. Search with NASADEM:

   ```
   python findaline.py --lat 39.7217 --lon 21.6306 --source nasadem --min-length 100
   ```

The key allows a limited number of downloads per day. Each request downloads one tile of about 100 × 100 km, which is kept in the `cache` folder, so a few searches use only a few requests. Keep the key to yourself; anyone who has it can use your daily limit.

## Next time
Open PowerShell or the Terminal and run:

**Windows:**
```
cd Find-a-Line
.\venv\Scripts\Activate.ps1
python findaline.py --lat ... --lon ...
```

**macOS:**
```
cd Find-a-Line
source venv/bin/activate
python3 findaline.py --lat ... --lon ...
```

## Updating to a newer version
With the environment active (as above):

```
git pull
pip install -r requirements.txt
```

## If something goes wrong
- **`python` is not recognised** (Windows): Python was installed without "Add python.exe to PATH". Run the installer again, choose *Modify* and tick *Add Python to environment variables*, then open a new PowerShell window.
- **It takes too long:** use a smaller `--radius`, a higher `--slope-threshold`, or a coarser `--resolution 5`. You can stop the script at any time with Ctrl+C.
- **"No lines found":** try a lower `--slope-threshold`, a larger `--radius` or other line lengths. Outside Poland and Malta the data is less detailed, so try longer lines, for example `--min-length 100`.
- **Download errors:** the data servers are sometimes busy. Run the same command again; what was already downloaded is kept.
- **Not enough disk space:** you can delete the `cache` folder (it is downloaded again when needed) and old searches in the `runs` folder.
