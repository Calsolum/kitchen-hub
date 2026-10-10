// Copy this file to config.local.js (same directory as dashboard.html,
// gitignored -- never commit the real one) and fill in your own values.
// dashboard.html loads this via a plain <script src> tag before it reads
// any of these, and falls back to harmless placeholder strings if the file
// is missing, so a fresh checkout still loads without one.
window.KITCHEN_CONFIG = {
  // Grocy > Settings > API keys
  GROCY_KEY: "",
  // Mealie > user menu > API tokens
  MEALIE_TOKEN: "",
  // https://openweathermap.org/api
  WEATHER_KEY: "",
  // The YouTube playlist the dashboard's YouTube tab plays: the "list=" value from a playlist URL.
  // Kept here, not in dashboard.html, so an unlisted playlist's id never lands in the public repo.
  YOUTUBE_PLAYLIST_ID: "",
};
