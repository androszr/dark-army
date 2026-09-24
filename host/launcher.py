# host/launcher.py — py2app entry point
# Imports the menubar app as a package so relative imports work.
import sys

# Never write bytecode inside the signed bundle. py2app's main stub already
# exports PYTHONDONTWRITEBYTECODE=1; this states the same rule where Dark
# Army's own code begins, so a module shipped without a cache writes nothing.
sys.dont_write_bytecode = True

from dark_army_menubar.app import main

main()
