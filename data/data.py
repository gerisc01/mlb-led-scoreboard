import time
from datetime import datetime, timedelta

import statsapi

import data.layout as layout
import data.teams
import debug
from data.final import Final
from data.headlines import Headlines
from data.pregame import Pregame
from data.scoreboard import Scoreboard
from data.standings import Standings
from data.status import Status
from data.weather import Weather

NETWORK_RETRY_SLEEP_TIME = 10.0


FIELDS = (
    "gameData,game,id,datetime,dateTime,flags,noHitter,perfectGame,status,detailedState,probablePitchers,teams,"
    + "home,away,abbreviation,teamName,players,id,boxscoreName,liveData,decisions,winner,loser,save,id,fullName,"
    + "linescore,outs,balls,strikes,note,inningState,currentInning,currentInningOrdinal,offense,batter,inHole,onDeck,"
    + "first,second,third,defense,pitcher,boxscore,teams,runs,players,seasonStats,pitching,wins,losses,saves,era"
)


class Data:
    def __init__(self, config):
        # Save the parsed config
        self.config = config

        # Parse today's date and see if we should use today or yesterday
        self.today = self.__parse_today()

        # Flag to determine when to refresh data
        self.needs_refresh = True

        # What game do we want to start on?
        self.current_game_index = 0
        self.current_division_index = 0

        # Fetch the games for today
        self.refresh_games()
        self.current_game_index = self.game_index_for_preferred_team()

        # Fetch all standings data for today
        # (Good to have in case we add a standings screen while rotating scores)
        self.refresh_standings()

        # Network status state
        self.network_issues = False

        # Weather info
        self.weather = Weather(
            self.config.weather_apikey,
            self.config.weather_location,
            self.config.weather_metric_units,
        )

        # News headlines
        self.headlines = Headlines(self.config)

    #
    # Date

    def __parse_today(self):
        if self.config.demo_date:
            today = datetime.strptime(self.config.demo_date, "%Y-%m-%d")
        else:
            today = datetime.today()
            end_of_day = datetime.strptime(self.config.end_of_day, "%H:%M").replace(
                year=today.year, month=today.month, day=today.day
            )
            if end_of_day > datetime.now():
                today -= timedelta(days=1)
        return today

    def date(self):
        return self.today.strftime("%Y-%m-%d")

    def refresh_standings(self):
        try:
            debug.log("Refreshing standings for %s", self.date())
            self.standings = Standings(self.today)
        except:
            debug.error("Failed to refresh standings.")

    def refresh_games(self):
        debug.log("Updating games for %s", self.date())
        self.games = self.__get_games()
        self.games_refresh_time = time.time()

    def __get_games(self):
        try:
            all_games = statsapi.schedule(self.date())
        except:
            self.network_issues = True
            debug.error("Networking error while refreshing the master list of games.")
        else:
            self.network_issues = False
            if self.config.rotation_only_preferred:
                return Data.__filter_list_of_games(all_games, self.config.preferred_teams)
            else:
                return all_games

        return []

    def refresh_game_data(self):
        try:
            self.game_data = statsapi.get("game", {"gamePk": self.current_game()["game_id"], "fields": FIELDS})
        except:
            self.network_issues = True
            debug.error("Networking Error while refreshing the current game_data.")
        else:
            self.__update_layout_state()
            self.needs_refresh = False
            self.print_game_data_debug()
            self.network_issues = False
            return

        # just move on to the next game
        if self.config.rotation_enabled:
            self.advance_to_next_game()

    def refresh_weather(self):
        self.network_issues = not self.weather.update()

    def refresh_news_ticker(self):
        self.network_issues = not self.headlines.update()

    # Will use a network call to fetch the preferred team's game game_data
    def fetch_preferred_team_game_data(self):
        if not self.is_offday_for_preferred_team():
            game = self.games[self.game_index_for_preferred_team()]
            try:
                game_data = statsapi.get("game", {"gamePk": game["game_id"], "fields": FIELDS})
            except:
                game_data = self.game_data
                self.network_issues = True
                debug.error("Failed to refresh game data for preferred team")
            else:
                self.network_issues = False
                debug.log(
                    "Preferred Team's Game Status: %s, %s %d",
                    game_data["gameData"]["status"]["detailedState"],
                    game_data["liveData"]["linescore"].get("inningState", "Top"),
                    game_data["liveData"]["linescore"].get("currentInning", 0),
                )
            return game_data

    def __update_layout_state(self):
        self.config.layout.set_state()
        if self.game_data["gameData"]["status"]["detailedState"] == Status.WARMUP:
            self.config.layout.set_state(layout.LAYOUT_STATE_WARMUP)

        if self.game_data["gameData"]["flags"]["noHitter"]:
            self.config.layout.set_state(layout.LAYOUT_STATE_NOHIT)

        if self.game_data["gameData"]["flags"]["perfectGame"]:
            self.config.layout.set_state(layout.LAYOUT_STATE_PERFECT)

    #
    # Standings

    def standings_for_preferred_division(self):
        return self.__standings_for(self.config.preferred_divisions[0])

    def __standings_for(self, division_name):
        return next(division for division in self.standings.divisions if division.name == division_name)

    def current_standings(self):
        return self.__standings_for(self.config.preferred_divisions[self.current_division_index])

    def advance_to_next_standings(self):
        self.current_division_index = self.__next_division_index()
        return self.current_standings()

    def __next_division_index(self):
        counter = self.current_division_index + 1
        if counter >= len(self.config.preferred_divisions):
            counter = 0
        return counter

    #
    # Games

    def current_game(self):
        return self.games[self.current_game_index]

    def advance_to_next_game(self):
        # We only need to check the preferred team's game status if we're
        # rotating during mid-innings
        if self.config.rotation_preferred_team_live_mid_inning and not self.is_offday_for_preferred_team():
            preferred_game_data = self.fetch_preferred_team_game_data()
            if self.network_issues or (
                Status.is_live(preferred_game_data["gameData"]["status"]["detailedState"])
                and not Status.is_inning_break(preferred_game_data["liveData"]["linescore"]["inningState"])
            ):
                self.current_game_index = self.game_index_for_preferred_team()
                self.game_data = preferred_game_data
                self.needs_refresh = False
                self.__update_layout_state()
                self.print_game_data_debug()
                debug.log("Moving to preferred game, index: %d", self.current_game_index)
                return self.current_game()
        self.current_game_index = self.__next_game_index()
        return self.current_game()

    def game_index_for_preferred_team(self):
        if self.config.preferred_teams:
            return self.__game_index_for(self.config.preferred_teams[0])
        else:
            return self.current_game_index

    @classmethod
    def __filter_list_of_games(cls, games, filter_teams):
        teams = [data.teams.TEAM_FULL[t] for t in filter_teams]
        return list(game for game in games if set([game["away_name"], game["home_name"]]).intersection(set(teams)))

    def __game_index_for(self, team_name):
        team_name = data.teams.TEAM_FULL[team_name]
        team_index = self.current_game_index
        team_idxs = [i for i, game in enumerate(self.games) if team_name in [game["away_name"], game["home_name"]]]
        if len(team_idxs) > 0:
            team_index = next(
                (i for i in team_idxs if Status.is_live(self.games[i]["status"])),
                team_idxs[0],
            )

        return team_index

    def __next_game_index(self):
        counter = self.current_game_index + 1
        if counter >= len(self.games):
            counter = 0
        debug.log("Going to game index %d", counter)
        return counter

    #
    # Offdays

    def is_offday_for_preferred_team(self):
        if self.config.preferred_teams:
            return not any(
                data.teams.TEAM_FULL[self.config.preferred_teams[0]] in [game["away_name"], game["home_name"]]
                for game in self.games
            )
        else:
            return True

    def is_offday(self):
        return not len(self.games)

    def games_live(self):
        return any(
            Status.is_fresh(g["status"]) or (Status.is_live(g["status"]) or g["status"] == Status.WARMUP)
            for g in self.games
        )

    #
    # Debug info

    def print_game_data_debug(self):
        debug.log("Game Data Refreshed: %s", self.game_data["gameData"]["game"]["id"])
        debug.log("Pre: %s", Pregame(self.game_data, self.config.time_format))
        debug.log("Live: %s", Scoreboard(self.game_data))
        debug.log("Final: %s", Final(self.game_data))
