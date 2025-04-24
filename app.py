import logging
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from flask_sqlalchemy import SQLAlchemy
import requests
import os
import re
from dotenv import load_dotenv  # Add for environment variables

# Load environment variables
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', os.urandom(24).hex())  # Use env var, fallback to random for local
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///users.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
NHL_API_URL = "https://api-web.nhle.com/v1/"

# Setup logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

db = SQLAlchemy(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"


# Database model
class User(UserMixin, db.Model):
    id = db.Column(db.String(50), primary_key=True)
    password = db.Column(db.String(100), nullable=False)
    favorite_west_team = db.Column(db.String(10))  # e.g., "COL"
    favorite_east_team = db.Column(db.String(10))  # e.g., "TBL"


# Create database
with app.app_context():
    db.create_all()


# Fetch playoff teams by conference
def fetch_nhl_teams(season="20242025"):
    try:
        response = requests.get(f"{NHL_API_URL}standings/now", timeout=5)
        response.raise_for_status()
        standings = response.json().get("standings", [])

        west_teams = []
        east_teams = []
        for team in standings:
            team_data = {
                "abbr": team.get("teamAbbrev", {}).get("default", ""),
                "name": team.get("teamName", {}).get("default", ""),
                "conference": team.get("conferenceName", "")
            }
            if team_data["conference"] == "Western" and len(west_teams) < 8:
                west_teams.append(team_data)
            elif team_data["conference"] == "Eastern" and len(east_teams) < 8:
                east_teams.append(team_data)

        return west_teams, east_teams
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch teams: {e}")
        return [], []


# Fetch all playoff series
def get_all_playoff_series(season="20242025"):
    series_letters = ['a', 'b', 'c', 'd', 'e', 'f', 'g', 'h']  # First-round series
    all_series = []
    for letter in series_letters:
        url = f"{NHL_API_URL}schedule/playoff-series/{season}/{letter.lower()}"
        try:
            response = requests.get(url, timeout=5)
            response.raise_for_status()
            series_data = response.json()
            all_series.append(series_data)
            logger.debug(f"Fetched series {letter}: {series_data.get('seriesLetter')}")
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to fetch series {letter}: {e}")
    return all_series


# Selects the series for teams that are saved to the user's profile
def get_series_for_teams(all_series, selected_teams):
    selected_series = []
    for series in all_series:
        top_team_abbr = series.get('topSeedTeam', {}).get('abbrev', '')
        bottom_team_abbr = series.get('bottomSeedTeam', {}).get('abbrev', '')
        if top_team_abbr in selected_teams or bottom_team_abbr in selected_teams:
            selected_series.append({
                'series_letter': series.get('seriesLetter', ''),
                'top_team': series.get('topSeedTeam', {}).get('abbrev', ''),
                'bottom_team': series.get('bottomSeedTeam', {}).get('abbrev', ''),
                'series_status': f"{series.get('topSeedTeam', {}).get('seriesWins', 0)}-{series.get('bottomSeedTeam', {}).get('seriesWins', 0)}",
                'games': [
                    {
                        'id': str(game.get('id', '')),
                        'gameNumber': game.get('gameNumber', 'TBD'),
                        'date': datetime.strptime(game.get('startTimeUTC', '').split('T')[0], '%Y-%m-%d').strftime(
                            '%m-%d-%Y') if 'T' in game.get('startTimeUTC', '') else 'TBD',
                        'home_team': game.get('homeTeam', {}).get('abbrev', ''),
                        'away_team': game.get('awayTeam', {}).get('abbrev', ''),
                        'home_score': game.get('homeTeam', {}).get('score', 0),
                        'away_score': game.get('awayTeam', {}).get('score', 0),
                        'state': "Completed" if game.get('gameState', 'TBD') == "OFF" else "Upcoming" if game.get(
                            'gameState', 'TBD') == "FUT" else game.get('gameState', 'TBD'),
                        'tv': game.get('tvBroadcasts', [{}])[0].get('network', 'N/A') if game.get(
                            'tvBroadcasts') else 'N/A',
                        'start_time': game.get('startTimeUTC', '') if game.get('startTimeUTC', '') else 'TBD',
                        'winner': game.get('homeTeam', {}).get('abbrev', '') if game.get('homeTeam', {}).get('score',
                                                                                                             0) > game.get(
                            'awayTeam', {}).get('score', 0) else game.get('awayTeam', {}).get('abbrev', '') if game.get(
                            'awayTeam', {}).get('score', 0) > game.get('homeTeam', {}).get('score', 0) else None
                    } for game in series.get('games', [])
                ]
            })
            logger.debug(f"Found series for teams {top_team_abbr}/{bottom_team_abbr}")
    return selected_series


# Fetches live game data if one is currently live
def fetch_live_game_data(teams, date):
    """Fetch live game data for the given teams on the specified date range."""
    try:
        # Check today and tomorrow to find live games
        dates = [date, date + timedelta(days=1)]
        for check_date in dates:
            date_str = check_date.strftime('%Y-%m-%d')
            schedule_url = f"{NHL_API_URL}schedule/{date_str}"
            logger.debug(f"Fetching schedule for {date_str} with teams {teams}")
            response = requests.get(schedule_url, timeout=5)
            response.raise_for_status()
            games = response.json().get("games", [])
            logger.debug(f"Found {len(games)} games on {date_str}: {games}")

            for game in games:
                home_abbrev = game.get("homeTeam", {}).get("abbrev", "")
                away_abbrev = game.get("awayTeam", {}).get("abbrev", "")
                logger.debug(f"Checking game: home={home_abbrev}, away={away_abbrev}, state={game.get('gameState')}")
                if game.get("gameState") == "LIVE" and any(team in [home_abbrev, away_abbrev] for team in teams):
                    game_id = str(game.get("id", ""))
                    boxscore_url = f"{NHL_API_URL}gamecenter/{game_id}/boxscore"
                    logger.debug(f"Fetching live boxscore for game {game_id}")
                    boxscore_response = requests.get(boxscore_url, timeout=5)
                    boxscore_response.raise_for_status()
                    boxscore = boxscore_response.json()

                    # Extract live game data
                    live_data = {
                        "game_id": game_id,
                        "teams": f"{boxscore.get('awayTeam', {}).get('abbrev', 'TBD')} @ {boxscore.get('homeTeam', {}).get('abbrev', 'TBD')}",
                        "score": f"{boxscore.get('awayTeam', {}).get('score', 0)} - {boxscore.get('homeTeam', {}).get('score', 0)}",
                        "period": boxscore.get('periodDescriptor', {}).get('number', 'TBD'),
                        "clock": boxscore.get('clock', {}).get('timeRemaining', 'TBD'),
                        "shots": f"{boxscore.get('awayTeam', {}).get('sog', 0)} - {boxscore.get('homeTeam', {}).get('sog', 0)}",
                        "events": []
                    }
                    team = home_abbrev if home_abbrev in teams else away_abbrev
                    logger.debug(f"Live game found: team={team}, data={live_data}")
                    return team, live_data

        # Mock live game for testing
        logger.debug(f"No live games found for {teams} on {dates}, using mock data")
        if teams:
            mock_team = teams[0]
            mock_data = {
                "game_id": "2024030999",
                "teams": f"TST @ {mock_team}",
                "score": "2 - 1",
                "period": "2",
                "clock": "12:34",
                "shots": "15 - 10",
                "events": []
            }
            logger.debug(f"Mock live game: team={mock_team}, data={mock_data}")
            return mock_team, mock_data
        logger.debug("No teams provided for mock data")
        return None, None
    except requests.RequestException as e:
        logger.error(f"Failed to fetch live game data: {e}")
        return None, None


@login_manager.user_loader
def load_user(user_id):
    logger.debug(f"Loading user: {user_id}")
    return db.session.get(User, user_id)


# Both are root routes for logging in
@app.route("/", methods=["GET", "POST"])
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        logger.debug("Processing POST request for login")
        username = request.form.get("username")
        password = request.form.get("password")
        logger.debug(f"Login attempt: username={username}, password_length={len(password or '')}")

        if not username or not password:
            logger.debug("Missing login credentials")
            flash("Username and password are required.", "error")
        else:
            user = db.session.get(User, username)
            if not user:
                logger.debug(f"Login failed: Username {username} not found")
                flash("Username does not exist.", "error")
            elif user.password != password:
                logger.debug(f"Login failed: Incorrect password for {username}")
                flash("Incorrect password.", "error")
            else:
                login_user(user)
                logger.debug(f"User {username} logged in successfully")
                flash("Login successful!", "success")
                return redirect(url_for("dashboard"))
    logger.debug("Rendering login page")
    return render_template("login.html")


# Sign up route with regex for user credentials
@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        logger.debug("Processing POST request for signup")
        username = request.form.get("username")
        password = request.form.get("password")
        confirm_password = request.form.get("confirm_password")
        logger.debug(
            f"Signup attempt: username={username}, password_length={len(password or '')}, confirm_password_length={len(confirm_password or '')}")

        # Validation checks
        if not username or not password or not confirm_password:
            logger.debug("Missing form fields")
            flash("All fields are required.", "error")
        elif len(username) < 3:
            logger.debug("Username too short")
            flash("Username must be at least 3 characters.", "error")
        elif not re.match(r"^[a-zA-Z0-9]+$", username):
            logger.debug("Invalid username format")
            flash("Username must be alphanumeric.", "error")
        elif len(password) < 6:
            logger.debug("Password too short")
            flash("Password must be at least 6 characters.", "error")
        elif password != confirm_password:
            logger.debug("Passwords do not match")
            flash("Passwords do not match.", "error")
        elif db.session.get(User, username):
            logger.debug("Username already exists")
            flash("Username already exists.", "error")
        else:
            new_user = User(id=username, password=password)
            db.session.add(new_user)
            try:
                db.session.commit()
                logger.debug(f"User {username} created successfully")
                flash("Signup successful! Please log in.", "success")
                return redirect(url_for("login"))
            except Exception as e:
                db.session.rollback()
                logger.error(f"Signup error: {str(e)}")
                flash(f"Signup error: {str(e)}", "error")
    logger.debug("Rendering signup page")
    return render_template("signup.html")


# Check db to see if user already exists
@app.route("/check_username", methods=["POST"])
def check_username():
    username = request.form.get("username")
    mode = request.form.get("mode", "signup")
    logger.debug(f"Checking username: {username}, mode={mode}, request_origin={request.headers.get('User-Agent')}")
    if not username:
        logger.debug("Username check: Missing username")
        return jsonify({"available": False, "message": "Username is required."})
    user_exists = bool(db.session.get(User, username))
    if mode == "signup":
        if user_exists:
            logger.debug(f"Username check: {username} already taken")
            return jsonify({"available": False, "message": "Username is already taken."})
        logger.debug(f"Username check: {username} available")
        return jsonify({"available": True, "message": "Username is available."})
    else:  # mode == "login"
        if user_exists:
            logger.debug(f"Username check: {username} exists")
            return jsonify({"available": True, "message": "Username exists."})
        logger.debug(f"Username check: {username} does not exist")
        return jsonify({"available": False, "message": "Username does not exist."})


@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    west_teams, east_teams = fetch_nhl_teams()
    if request.method == "POST":
        logger.debug("Processing POST request for profile")
        favorite_west_team = request.form.get("favorite_west_team")
        favorite_east_team = request.form.get("favorite_east_team")

        if not favorite_west_team or not favorite_east_team:
            flash("Please select one team from each conference.", "error")
        elif favorite_west_team == favorite_east_team:
            flash("Please select different teams from each conference.", "error")
        else:
            user = db.session.get(User, current_user.id)
            user.favorite_west_team = favorite_west_team
            user.favorite_east_team = favorite_east_team
            try:
                db.session.commit()
                logger.debug(f"Profile updated for user: {current_user.id}")
                flash("Profile updated successfully!", "success")
                return redirect(url_for("dashboard"))
            except Exception as e:
                db.session.rollback()
                logger.error(f"Profile update error: {str(e)}")
                flash(f"Profile update error: {str(e)}")

    logger.debug("Rendering profile page")
    return render_template("profile.html", west_teams=west_teams, east_teams=east_teams)


@app.route("/dashboard")
@login_required
def dashboard():
    try:
        logger.debug(f"Fetching dashboard for user: {current_user.id}")
        user = db.session.get(User, current_user.id)
        if not user:
            logger.error("User not found")
            flash("User not found.", "error")
            return redirect(url_for("login"))

        if not user.favorite_west_team or not user.favorite_east_team:
            logger.debug("No favorite teams selected")
            flash("Please select your favorite teams in your profile.", "error")
            return redirect(url_for("profile"))

        teams = [user.favorite_west_team, user.favorite_east_team]
        all_series = get_all_playoff_series()
        series_to_display = get_series_for_teams(all_series, teams)

        # Fetch live game data for today
        west_live_team, west_live_game = fetch_live_game_data([user.favorite_west_team], datetime.now())
        east_live_team, east_live_game = fetch_live_game_data([user.favorite_east_team], datetime.now())

        # Fetch team info
        west_team_info = next((t for t in fetch_nhl_teams()[0] if t["abbr"] == user.favorite_west_team),
                              {"name": "Unknown", "abbr": "Unknown"})
        east_team_info = next((t for t in fetch_nhl_teams()[1] if t["abbr"] == user.favorite_east_team),
                              {"name": "Unknown", "abbr": "Unknown"})

        # Team logos
        west_logo_url = f"https://assets.nhle.com/logos/nhl/svg/{user.favorite_west_team}_light.svg"
        east_logo_url = f"https://assets.nhle.com/logos/nhl/svg/{user.favorite_east_team}_light.svg"

        logger.debug(
            f"Rendering dashboard with {len(series_to_display)} series, west_live={bool(west_live_game)}, east_live={bool(east_live_game)}")
        return render_template(
            "dashboard.html",
            west_team=west_team_info,
            east_team=east_team_info,
            series_to_display=series_to_display,
            west_logo_url=west_logo_url,
            east_logo_url=east_logo_url,
            west_live_game=west_live_game,
            east_live_game=east_live_game,
            current_date=datetime.now().strftime("%Y-%m-%d")
        )
    except Exception as e:
        logger.error(f"Unexpected dashboard error: {e}")
        flash(f"Unexpected error: {e}", "error")
        return render_template(
            "dashboard.html",
            west_team={"name": "Unknown", "abbr": "Unknown"},
            east_team={"name": "Unknown", "abbr": "Unknown"},
            series_to_display=[],
            west_logo_url="https://assets.nhle.com/logos/nhl/svg/NHL_light.svg",
            east_logo_url="https://assets.nhle.com/logos/nhl/svg/NHL_light.svg",
            west_live_game=None,
            east_live_game=None,
            current_date=datetime.now().strftime("%Y-%m-%d"),
            error=str(e)
        )


# Fetches game preview if available
@app.route("/game/<game_id>")
@login_required
def game_details(game_id):
    try:
        logger.debug(f"Fetching game details for game_id: {game_id}")
        boxscore_url = f"{NHL_API_URL}gamecenter/{game_id}/boxscore"
        response = requests.get(boxscore_url, timeout=5)
        response.raise_for_status()
        boxscore = response.json()
        game_state = boxscore.get("gameState", "LIVE")

        if game_state == "OFF":
            logger.debug("Rendering game stats")
            return render_template("game_stats.html", boxscore=boxscore)
        else:
            # Fetch all playoff series to find the game
            all_series = get_all_playoff_series()
            game = None
            for series in all_series:
                for g in series.get("games", []):
                    if str(g.get("id")) == game_id:
                        game = g
                        break
                if game:
                    break

            if game:
                preview_data = {
                    "teams": f"{game.get('awayTeam', {}).get('abbrev', 'TBD')} vs {game.get('homeTeam', {}).get('abbrev', 'TBD')}",
                    "date": datetime.strptime(game.get('startTimeUTC', '').split('T')[0], '%Y-%m-%d').strftime(
                        '%m-%d-%Y') if 'T' in game.get('startTimeUTC', '') else 'TBD',
                    "time": game.get('startTimeUTC', '') if game.get('startTimeUTC', '') else 'TBD',
                    "tv": game.get('tvBroadcasts', [{}])[0].get('network', 'N/A') if game.get(
                        'tvBroadcasts') else 'N/A',
                    "watch": "Stream on ESPN+"
                }
                logger.debug(f"Rendering game preview: {preview_data}")
                return render_template("game_preview.html", preview=preview_data)
            logger.debug("Game preview unavailable")
            return render_template("game_preview.html", error="Game preview unavailable")
    except requests.RequestException as e:
        logger.error(f"Game details error: {e}")
        flash(f"API error: {e}", "error")
        return render_template("game_preview.html", error=str(e))


@app.route("/logout")
@login_required
def logout():
    logger.debug("Logging out user")
    logout_user()
    flash("Logged out successfully", "success")
    return redirect(url_for("login"))


if __name__ == "__main__":
    app.run(debug=True)