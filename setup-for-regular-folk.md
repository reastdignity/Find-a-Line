### How to just get things running
Follow the steps below to set up this project locally using MacOS. Some commands may require elevated privileges which can be accessed by adding `sudo` to the beginning of the command, which will prompt you to enter your computer's password. If you get stuck you can probably figure things out with a little bit of research.

- Open your terminal using Command+Space and typing `terminal`
- Install homebrew `/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"`
- Install git `brew install git`
- Download this project `git clone https://github.com/ptarmagain/Find-a-Line.git`
- Move into this project `cd Find-a-Line`
- Create a virutal environment `python3 -m venv venv`
- Open the virtual environment `source venv/bin/activate`
- Install requirements `pip install -r requirements.txt`
- Download the raster files and move them to the same location as your project.
- The values within `findaline.py` for your `slope_file` and `elevation_file` need to be replaced. Run `pwd` to get the system path to the files.
- Once you have updated these values, start the app `python findaline.py`

This application can take awhile to run. If it is taking too much time, you will need to compromise by doing at least one of the following:
- Use smaller raster files
- Change your minimum or maximum line lengths 
