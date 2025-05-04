# Alex Schaffer
# 05/04/2025
# CS-115
# Final Project

import logging
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, flash, session
from flask_sqlalchemy import SQLAlchemy
import requests
import os
from dotenv import load_dotenv
from urllib.parse import urlencode
from authlib.integrations.flask_client import OAuth
from sqlalchemy.exc import OperationalError

# Load environment variables (for local development)
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY')
if not app.secret_key:
    raise ValueError("No SECRET_KEY set in environment variables")

# Database configuration
database_url = os.getenv('DATABASE_URL', 'sqlite:///users.db')
if database_url.startswith('postgresql://'):
    database_url = database_url.replace('postgresql://', 'postgresql+psycopg2://', 1) + '?sslmode=require'
app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_pre_ping': True,
    'pool_size': 5,
    'max_overflow': 10,
    'pool_timeout': 30,
}
app.config['SESSION_PERMANENT'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = True
NHL_API_URL = "https://api-web.nhle.com/v1/"

# Auth0 configuration
AUTH0_DOMAIN = os.getenv('AUTH0_DOMAIN')
AUTH0_CLIENT_ID = os.getenv('AUTH0_CLIENT_ID')
AUTH0_CLIENT_SECRET = os.getenv('AUTH0_CLIENT_SECRET')
AUTH0_CALLBACK_URL = os.getenv('AUTH0_CALLBACK_URL')

# Setup logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Initialize OAuth
oauth = OAuth(app)
oauth.register(
    name='auth0',
    client_id=AUTH0_CLIENT_ID,
    client_secret=AUTH0_CLIENT_SECRET,
    server_metadata_url=f'https://{AUTH0_DOMAIN}/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid profile email'},
)

db = SQLAlchemy(app)

# Database model
class User(db.Model):
    id = db.Column(db.String(100), primary_key=True)  # Auth0 sub (user_id)
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
            if team_data["conference"] == "Western" and len(west_teams) < 16:
                west_teams.append(team_data)
            elif team_data["conference"] == "Eastern" and len(east_teams) < 16:
                east_teams.append(team_data)

        return west_teams, east_teams
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch teams: {e}")
        return [], []

# Fetch all playoff series
def get_all_playoff_series(season="20242025"):
    series_letters = ['a', 'b', 'c', 'd', 'e', 'f', 'g', 'h', 'i', 'j', 'k', 'l', 'm', 'n', 'o', 'p']
    all_series = []
    for letter in series_letters:
        url = f"{NHL_API_URL}schedule/playoff-series/{season}/{letter.lower()}"
        try:
            response = requests.get(url, timeout=5)
            response.raise_for_status()
            series_data = response.json()
            if series_data.get('seriesLetter') and series_data.get('topSeedTeam', {}).get('abbrev') and series_data.get('bottomSeedTeam', {}).get('abbrev'):
                all_series.append(series_data)
                logger.debug(f"Fetched series {letter}: {series_data.get('seriesLetter')} with teams {series_data.get('topSeedTeam', {}).get('abbrev')} vs {series_data.get('bottomSeedTeam', {}).get('abbrev')}")
            else:
                logger.debug(f"Skipping series {letter} due to incomplete data: {series_data}")
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 404:
                logger.debug(f"Series {letter} not found: {e}")
                continue
            raise
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to fetch series {letter}: {e}")
    logger.debug(f"All series fetched: {len(all_series)} series")
    return all_series

# Selects the current active series for teams saved to the user's profile
def get_series_for_teams(all_series, selected_teams):
    selected_series = []
    selected_teams = [team.upper() for team in selected_teams]
    team_series = {team: None for team in selected_teams}  # Track latest active series per team

    for series in all_series:
        top_team_data = series.get('topSeedTeam', {})
        bottom_team_data = series.get('bottomSeedTeam', {})
        top_team_abbr = top_team_data.get('abbrev', '').upper()
        bottom_team_abbr = bottom_team_data.get('abbrev', '').upper()
        if not top_team_abbr or not bottom_team_abbr:
            logger.debug(f"Skipping series {series.get('seriesLetter', 'unknown')} due to missing team abbreviations")
            continue

        top_wins = top_team_data.get('seriesWins', 0)
        bottom_wins = bottom_team_data.get('seriesWins', 0)
        round = str(series.get('round', 'Unknown'))
        round_num = int(round) if round.isdigit() else 999

        # Process series for each selected team
        for team in selected_teams:
            if team not in [top_team_abbr, bottom_team_abbr]:
                continue

            # Skip series if the team has been eliminated
            if (team == top_team_abbr and bottom_wins >= 4) or (team == bottom_team_abbr and top_wins >= 4):
                continue

            # Check if the series is active (not completed)
            if top_wins < 4 and bottom_wins < 4:
                current_series = team_series[team]
                if not current_series or round_num > (int(current_series['round']) if current_series['round'].isdigit() else 999):
                    series_data = {
                        'series_letter': series.get('seriesLetter', ''),
                        'round': round,
                        'top_team': top_team_abbr,
                        'bottom_team': bottom_team_abbr,
                        'series_status': f"{top_wins}-{bottom_wins}",
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
                                'tv': game.get('tvBroadcasts', [{}])[0].get('network', 'N/A') if game.get('tvBroadcasts') else 'N/A',
                                'start_time': game.get('startTimeUTC', '') if game.get('startTimeUTC', '') else 'TBD',
                                'winner': game.get('homeTeam', {}).get('abbrev', '') if game.get('homeTeam', {}).get('score',
                                                                                                                    0) > game.get(
                                    'awayTeam', {}).get('score', 0) else game.get('awayTeam', {}).get('abbrev', '') if game.get(
                                    'awayTeam', {}).get('score', 0) > game.get('homeTeam', {}).get('score', 0) else None
                            } for game in series.get('games', [])
                        ]
                    }
                    team_series[team] = series_data
                    logger.debug(f"Updated active series for team {team}: {series_data}")

    selected_series = [series for series in team_series.values() if series]
    logger.debug(f"Selected series: {selected_series}")
    return selected_series

# Determine team status (eliminated, current round)
def get_team_status(all_series, team_abbr):
    status = {'eliminated': False, 'current_round': 'Not in Playoffs'}
    team_abbr = team_abbr.upper()
    max_round = 0
    for series in all_series:
        top_team_data = series.get('topSeedTeam', {})
        bottom_team_data = series.get('bottomSeedTeam', {})
        top_team = top_team_data.get('abbrev', '').upper()
        bottom_team = bottom_team_data.get('abbrev', '').upper()
        if not top_team or not bottom_team:
            logger.debug(f"Skipping series {series.get('seriesLetter', 'unknown')} in status check due to missing team data")
            continue

        top_wins = top_team_data.get('seriesWins', 0)
        bottom_wins = bottom_team_data.get('seriesWins', 0)
        round = str(series.get('round', 'Unknown'))
        round_num = int(round) if round.isdigit() else 999

        if team_abbr == top_team:
            if bottom_wins >= 4:
                status['eliminated'] = True
            elif round_num > max_round and top_wins < 4 and bottom_wins < 4:
                status['current_round'] = f"Round {round}" if round.isdigit() else round
                max_round = round_num
        elif team_abbr == bottom_team:
            if top_wins >= 4:
                status['eliminated'] = True
            elif round_num > max_round and top_wins < 4 and bottom_wins < 4:
                status['current_round'] = f"Round {round}" if round.isdigit() else round
                max_round = round_num

    return status

@app.route("/")
def index():
    logger.debug(f"Accessing root route, session={session.get('user_id')}")
    if 'user_id' in session:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))

@app.route("/login")
def login():
    logger.debug(f"Initiating Auth0 login")
    callback_url = AUTH0_CALLBACK_URL or url_for('callback', _external=True, _scheme='https')
    return oauth.auth0.authorize_redirect(redirect_uri=callback_url)

@app.route("/callback")
def callback():
    logger.debug(f"Handling Auth0 callback")
    try:
        token = oauth.auth0.authorize_access_token()
        user_info = token.get('userinfo')
        if user_info:
            session['user_id'] = user_info.get('sub')
            session['user_email'] = user_info.get('email', 'Unknown')
            try:
                user = db.session.get(User, session['user_id'])
                if not user:
                    user = User(id=session['user_id'])
                    db.session.add(user)
                    db.session.commit()
                    logger.debug(f"Created new user: {session['user_id']}")
                    session['new_user'] = True
                else:
                    logger.debug(f"Existing user logged in: {session['user_id']}")
                    session['new_user'] = False
            finally:
                db.session.remove()
            flash("Login successful!", "success")
            return redirect(url_for("dashboard"))
        flash("Authentication failed.", "error")
        return redirect(url_for("login"))
    except Exception as e:
        logger.error(f"Auth0 callback error: {e}")
        flash(f"Authentication error: {e}", "error")
        return redirect(url_for("login"))

@app.route("/logout")
def logout():
    logger.debug(f"Logging out user, session={session.get('user_id')}")
    session.clear()
    params = {'returnTo': url_for('index', _external=True), 'client_id': AUTH0_CLIENT_ID}
    return redirect(f"https://{AUTH0_DOMAIN}/v2/logout?" + urlencode(params))

@app.route("/profile", methods=["GET", "POST"])
def profile():
    if 'user_id' not in session:
        logger.debug("Unauthorized profile access")
        return redirect(url_for("login"))
    try:
        user = db.session.get(User, session['user_id'])
        if not user:
            logger.error(f"User not found: {session['user_id']}")
            flash("User not found.", "error")
            return redirect(url_for("login"))
        west_teams, east_teams = fetch_nhl_teams()
        if request.method == "POST":
            favorite_west_team = request.form.get("favorite_west_team")
            favorite_east_team = request.form.get("favorite_east_team")
            if not favorite_west_team or not favorite_east_team:
                flash("Please select one team from each conference.", "error")
            elif favorite_west_team == favorite_east_team:
                flash("Please select different teams from each conference.", "error")
            else:
                user.favorite_west_team = favorite_west_team
                user.favorite_east_team = favorite_east_team
                try:
                    db.session.commit()
                    logger.debug(f"Profile updated for user: {session['user_id']}")
                    flash("Profile updated successfully!", "success")
                    session.pop('new_user', None)
                    return redirect(url_for("dashboard"))
                except Exception as e:
                    db.session.rollback()
                    logger.error(f"Profile update error: {str(e)}")
                    flash(f"Profile update error: {str(e)}", "error")
        return render_template("profile.html", west_teams=west_teams, east_teams=east_teams,
                               user_email=session.get('user_email'))
    finally:
        db.session.remove()

@app.route("/dashboard")
def dashboard():
    if 'user_id' not in session:
        logger.debug("Unauthorized dashboard access")
        return redirect(url_for("login"))
    try:
        if session.get('new_user'):
            flash("Please select your favorite teams in your profile.", "info")
            return redirect(url_for("profile"))
        user = db.session.get(User, session['user_id'])
        if not user:
            logger.error("User not found")
            flash("User not found.", "error")
            return redirect(url_for("login"))
        if not user.favorite_west_team or not user.favorite_east_team:
            flash("Please select your favorite teams in your profile.", "info")
            return redirect(url_for("profile"))
        teams = [user.favorite_west_team, user.favorite_east_team]
        all_series = get_all_playoff_series()
        series_to_display = get_series_for_teams(all_series, teams)
        west_team_info = next((t for t in fetch_nhl_teams()[0] if t["abbr"] == user.favorite_west_team),
                              {"name": "Unknown", "abbr": "Unknown"})
        east_team_info = next((t for t in fetch_nhl_teams()[1] if t["abbr"] == user.favorite_east_team),
                              {"name": "Unknown", "abbr": "Unknown"})
        west_team_status = get_team_status(all_series, user.favorite_west_team)
        east_team_status = get_team_status(all_series, user.favorite_east_team)
        west_team_info.update(west_team_status)
        east_team_info.update(east_team_status)
        west_logo_url = f"https://assets.nhle.com/logos/nhl/svg/{user.favorite_west_team}_light.svg"
        east_logo_url = f"https://assets.nhle.com/logos/nhl/svg/{user.favorite_east_team}_light.svg"
        return render_template(
            "dashboard.html",
            west_team=west_team_info,
            east_team=east_team_info,
            series_to_display=series_to_display,
            west_logo_url=west_logo_url,
            east_logo_url=east_logo_url,
            current_date=datetime.now().strftime("%Y-%m-%d"),
            user_email=session.get('user_email')
        )
    except OperationalError as e:
        logger.error(f"Database operational error in dashboard: {e}")
        flash("Please select your favorite teams in your profile.", "info")
        return redirect(url_for("profile"))
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
            current_date=datetime.now().strftime("%Y-%m-%d"),
            error=str(e),
            user_email=session.get('user_email')
        )
    finally:
        db.session.remove()

@app.route("/game/<game_id>")
def game_details(game_id):
    if 'user_id' not in session:
        logger.debug("Unauthorized game details access")
        return redirect(url_for("login"))
    try:
        boxscore_url = f"{NHL_API_URL}gamecenter/{game_id}/boxscore"
        response = requests.get(boxscore_url, timeout=5)
        response.raise_for_status()
        boxscore = response.json()
        game_state = boxscore.get("gameState", "LIVE")
        if game_state == "OFF":
            return render_template("game_stats.html", boxscore=boxscore, user_email=session.get('user_email'))
        else:
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
                    "tv": game.get('tvBroadcasts', [{}])[0].get('network', 'N/A') if game.get('tvBroadcasts') else 'N/A',
                    "watch": "Stream on ESPN+"
                }
                return render_template("game_preview.html", preview=preview_data, user_email=session.get('user_email'))
            return render_template("game_preview.html", error="Game preview unavailable",
                                   user_email=session.get('user_email'))
    except requests.RequestException as e:
        logger.error(f"Game details error: {e}")
        flash(f"API error: {e}", "error")
        return render_template("game_preview.html", error=str(e), user_email=session.get('user_email'))
    finally:
        db.session.remove()

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))