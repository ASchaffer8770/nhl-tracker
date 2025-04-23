import time
import logging
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, flash
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from flask_sqlalchemy import SQLAlchemy
import requests
import os

app = Flask(__name__)
app.secret_key = os.urandom(24)
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
    for letter in series_letters:  # Fixed: Changed 'letters' to 'series_letters'
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

#Selects the series for teams that are saved to the user's profile
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
                        'date': datetime.strptime(game.get('startTimeUTC', '').split('T')[0], '%Y-%m-%d').strftime('%m-%d-%Y') if 'T' in game.get('startTimeUTC', '') else 'TBD',
                        'home_team': game.get('homeTeam', {}).get('abbrev', ''),
                        'away_team': game.get('awayTeam', {}).get('abbrev', ''),
                        'home_score': game.get('homeTeam', {}).get('score', 0),
                        'away_score': game.get('awayTeam', {}).get('score', 0),
                        'state': "Completed" if game.get('gameState', 'TBD') == "OFF" else "Upcoming" if game.get('gameState', 'TBD') == "FUT" else game.get('gameState', 'TBD'),
                        'tv': game.get('tvBroadcasts', [{}])[0].get('network', 'N/A') if game.get('tvBroadcasts') else 'N/A',
                        'start_time': game.get('startTimeUTC', '') if game.get('startTimeUTC', '') else 'TBD',
                        'winner': game.get('homeTeam', {}).get('abbrev', '') if game.get('homeTeam', {}).get('score', 0) > game.get('awayTeam', {}).get('score', 0) else game.get('awayTeam', {}).get('abbrev', '') if game.get('awayTeam', {}).get('score', 0) > game.get('homeTeam', {}).get('score', 0) else None
                    } for game in series.get('games', [])
                ]
            })
            logger.debug(f"Found series for teams {top_team_abbr}/{bottom_team_abbr}")
    return selected_series

@login_manager.user_loader
def load_user(user_id):
    logger.debug(f"Loading user: {user_id}")
    return db.session.get(User, user_id)


@app.route("/", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        logger.debug("Processing POST request for login")
        username = request.form.get("username")
        password = request.form.get("password")
        logger.debug(f"Attempting login for username: {username}")
        try:
            user = db.session.get(User, username)
            if user and user.password == password:
                logger.debug("Password match, logging in user")
                login_user(user)
                next_page = request.args.get('next', url_for('dashboard'))
                logger.debug(f"Redirecting to: {next_page}")
                return redirect(next_page)
            else:
                logger.debug("Invalid credentials")
                flash("Invalid credentials")
        except Exception as e:
            logger.error(f"Login error: {str(e)}")
            flash(f"Login error: {str(e)}")
    logger.debug("Rendering login page")
    return render_template("login.html")


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        logger.debug("Processing POST request for signup")
        username = request.form.get("username")
        password = request.form.get("password")
        if not db.session.get(User, username):
            new_user = User(id=username, password=password)
            db.session.add(new_user)
            try:
                db.session.commit()
                logger.debug(f"User {username} created")
                flash("Signup successful! Please log in.")
                return redirect(url_for("login"))
            except Exception as e:
                db.session.rollback()
                logger.error(f"Signup error: {str(e)}")
                flash(f"Signup error: {str(e)}")
        else:
            logger.debug("Username already exists")
            flash("Username already exists")
    logger.debug("Rendering signup page")
    return render_template("signup.html")


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

        # Fetch team info
        west_team_info = next((t for t in fetch_nhl_teams()[0] if t["abbr"] == user.favorite_west_team),
                              {"name": "Unknown"})
        east_team_info = next((t for t in fetch_nhl_teams()[1] if t["abbr"] == user.favorite_east_team),
                              {"name": "Unknown"})

        # Team logos
        west_logo_url = f"https://assets.nhle.com/logos/nhl/svg/{user.favorite_west_team}_light.svg"
        east_logo_url = f"https://assets.nhle.com/logos/nhl/svg/{user.favorite_east_team}_light.svg"

        logger.debug(f"Rendering dashboard with {len(series_to_display)} series")
        return render_template(
            "dashboard.html",
            west_team=west_team_info,
            east_team=east_team_info,
            series_to_display=series_to_display,
            west_logo_url=west_logo_url,
            east_logo_url=east_logo_url,
            current_date=datetime.now().strftime("%Y-%m-%d")
        )
    except Exception as e:
        logger.error(f"Unexpected dashboard error: {e}")
        flash(f"Unexpected error: {e}", "error")
        return render_template(
            "dashboard.html",
            west_team={"name": "Unknown"},
            east_team={"name": "Unknown"},
            series_to_display=[],
            west_logo_url="https://assets.nhle.com/logos/nhl/svg/NHL_light.svg",
            east_logo_url="https://assets.nhle.com/logos/nhl/svg/NHL_light.svg",
            current_date=datetime.now().strftime("%Y-%m-%d"),
            error=str(e)
        )

#Fetches game preview if available
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