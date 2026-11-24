# Wallpaper AI Usage

Put Codex and Claude Code usage in the bottom-right corner of your Wallpaper Engine Scene. The panel shows the percentage used and the time until reset for the 5-hour and 7-day windows. You can change the panel background opacity in the wallpaper settings.

The installer makes a local copy of the Scene you pick. Wallpaper Engine draws the panel, and the two CLIs update it through their existing hooks.

## What you need

- Wallpaper Engine on Windows, with a 2D Scene selected for setup
- WSL with Python 3.10 or later and Windows interop enabled
- Codex CLI signed in with ChatGPT and Claude Code signed in to a plan that reports usage limits. You can also set up either tool on its own.

Run the commands below from WSL. Open the Wallpaper Engine editor once so it creates the local projects folder.

## Add the panel

1. In Wallpaper Engine, right-click the Scene and choose **Open in Explorer**. Use that folder as the source. Editable Scene folders and common Workshop Scene packages both work.
2. Clone this repository and run the installer. Replace both example paths with yours. Use the Wallpaper Engine monitor number from its installation folder's config file if you want a screen other than **Monitor0**.

   ~~~sh
   git clone https://github.com/KageRyo/wallpaper-ai-usage.git
   cd wallpaper-ai-usage
   python3 wallpaper_usage.py install '/mnt/c/path/to/source-wallpaper' \
     --engine '/mnt/c/Program Files (x86)/Steam/steamapps/common/wallpaper_engine' \
     --monitor Monitor0
   ~~~

3. In Wallpaper Engine, apply the copy whose title ends in **AI Usage** to that screen. Find the slider under **Usage panel background opacity**.

## Connect Codex and Claude Code

Print the settings for your checkout and install path.

~~~sh
python3 wallpaper_usage.py hooks
~~~

- **Codex.** Merge the two entries into the **hooks** object in **~/.codex/hooks.json**, or create the file with the printed example. Open **/hooks** in Codex and trust the two commands. They run when a session starts and after a model response, with at least 12 seconds between account queries.
- **Claude Code.** Add the printed **statusLine** entry to **~/.claude/settings.json**. Claude Code passes the latest available usage to the panel after an API response. If you have a custom status line, run the example below with your existing **command** value before copying the settings. Your status line will still receive the same input.

~~~sh
python3 wallpaper_usage.py hooks --claude-command 'bash ~/.claude/my-statusline.sh'
~~~

For an immediate Codex refresh, run **python3 wallpaper_usage.py codex --force** after applying the copy. Claude's row fills in after Claude Code provides usage. The panel uses the reported reset times for its countdowns.

## Remove the panel

Apply your original wallpaper in Wallpaper Engine. Remove the two Codex entries and restore your previous Claude Code status line, then remove the local wallpaper copy and the **~/.config/wallpaper-ai-usage** folder. Keep this checkout at its original path while the hooks are in use.

## Development

Run **python3 -m unittest discover -s tests** from the checkout. The Python tests use temporary Scene and configuration files.

## License

[MIT](LICENSE)
