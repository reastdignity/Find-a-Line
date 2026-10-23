### How to just get things running
Follow the steps below for your system to set up this project locally. If you get stuck you can probably figure things out with a little bit of research.

You only need to do the setup once. After that, skip to [Finding lines](#finding-lines).

#### macOS
Some commands may require elevated privileges which can be accessed by adding `sudo` to the beginning of the command, which will prompt you to enter your computer's password.

- Open your terminal using Command+Space and typing `terminal`
- Install homebrew `/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"`
- Install git `brew install git`
- Download this project `git clone https://github.com/ptarmagain/Find-a-Line.git`
- Move into this project `cd Find-a-Line`
- Create a virtual environment `python3 -m venv venv`
- Open the virtual environment `source venv/bin/activate`
- Install requirements `pip install -r requirements.txt`

Next time you open the terminal, just run `cd Find-a-Line` and `source venv/bin/activate` before finding lines.

#### Windows
- Install Python from [python.org/downloads](https://www.python.org/downloads/). In the first window of the installer, tick **"Add python.exe to PATH"** before clicking *Install Now*.
- Install Git from [git-scm.com/downloads/win](https://git-scm.com/downloads/win). The default options are fine.
- Open PowerShell: press the Windows key, type `powershell` and press Enter
- Download this project `git clone https://github.com/ptarmagain/Find-a-Line.git`
- Move into this project `cd Find-a-Line`
- Create a virtual environment `python -m venv venv`
- Open the virtual environment `.\venv\Scripts\Activate.ps1`
  - If you get an error saying that running scripts is disabled, run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, answer `Y`, and try again.
- Install requirements `pip install -r requirements.txt`

Next time you open PowerShell, just run `cd Find-a-Line` and `.\venv\Scripts\Activate.ps1` before finding lines.

### Finding lines
- Pick the centre of the area you want to search. The easiest way is Google Maps: right-click a spot and click the coordinates at the top of the menu to copy them (for example `49.2320, 19.9817`).
- Start the app with those coordinates. By default it searches within 1 km of that point:

  `python findaline.py --lat 49.2320 --lon 19.9817`

  To search a larger area, add for example `--radius 2000` (in metres).

- The elevation data is downloaded automatically from geoportal.gov.pl (only the first time for a given area) and the search starts.
- When it is done, the coordinates of the lines are printed, a map opens in a new window, and an interactive map is saved. Open `findaline_map.html` from the project folder in your web browser, click a line to see its details and its terrain profile, and switch to the orthophoto (aerial photos) in the top-right corner.
- Aerial photos around the found lines are saved too, so you can look at them later without internet. The first time this can take a while; add `--no-ortho-cache` to skip it.
- Every run is kept. To go back to an earlier one, pick it from the list in the top-left corner of the map, or open `runs/index.html`, which lists all runs with their maps and results (CSV files open in Excel).
- Similar lines in the same spot are shown as one line. Use the panel in the bottom-left corner of the map to change how they are grouped or to show every line.

Lines that pass too close to trees or buildings are shown in orange. To leave them out instead, add `--nmpt reject`; to skip this check (faster, less to download), add `--nmpt off`. To see all options, run `python findaline.py --help`.

This application can take awhile to run. Before a long run it shows an estimate and asks whether to continue. If it is taking too much time, you will need to compromise by doing at least one of the following:
- Use a smaller area (`--radius`)
- Use a coarser resolution, for example `--resolution 5`
- Change your minimum or maximum line lengths (`--min-length`, `--max-length`)
