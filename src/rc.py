# -*- coding: utf-8 -*-

# This file is part of Recent changes Goat compatible Discord webhook (RcGcDw).

# RcGcDw is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# RcGcDw is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with RcGcDw.  If not, see <http://www.gnu.org/licenses/>.

import re
import sys
import time
import logging
import requests
import src.api.client
from bs4 import BeautifulSoup

from src.configloader import settings
from src.misc import WIKI_SCRIPT_PATH, WIKI_API_PATH, datafile, send_simple, safe_read, LinkParser, \
	AUTO_SUPPRESSION_ENABLED, parse_mw_request_info
from src.discord.queue import messagequeue
from src.exceptions import MWError, BadRequest, ClientError, ServerError, MediaWikiError
from src.session import session
from src.api.context import Context
from typing import Union
# from src.rc_formatters import compact_formatter, embed_formatter, compact_abuselog_formatter, embed_abuselog_formatter
from src.i18n import rc
from collections import OrderedDict

_ = rc.gettext

storage = datafile

logger = logging.getLogger("rcgcdw.rc")

LinkParser = LinkParser()

class Wiki(object):
	"""Store verious data and functions related to wiki and fetching of Recent Changes"""
	def __init__(self):
		self.map_ips = {}
		self.downtimecredibility = 0
		self.last_downtime = 0
		self.tags = {}
		self.groups = {}
		self.streak = -1
		self.mw_messages = {}
		self.namespaces = None
		self.session = session
		self.logged_in = False
		self.initial_run_complete = False

	@staticmethod
	def handle_mw_errors(request):
		if "errors" in request:
			logger.error(request["errors"])
			raise MWError
		return request

	def log_in(self):
		# session.cookies.clear()
		if '@' not in settings["wiki_bot_login"]:
			logger.error(
				"Please provide proper nickname for login from {wiki}Special:BotPasswords".format(
					wiki=WIKI_SCRIPT_PATH))
			return
		if len(settings["wiki_bot_password"]) != 32:
			logger.error(
				"Password seems incorrect. It should be 32 characters long! Grab it from {wiki}Special:BotPasswords".format(
					wiki=WIKI_SCRIPT_PATH))
			return
		logger.info("Trying to log in to {wiki}...".format(wiki=WIKI_SCRIPT_PATH))
		try:
			response = self.handle_mw_errors(
				self.session.post(WIKI_API_PATH,
				                  data={'action': 'query', 'format': 'json', 'utf8': '', 'meta': 'tokens',
				                        'type': 'login'}))
			response = self.handle_mw_errors(
				self.session.post(WIKI_API_PATH,
				                  data={'action': 'login', 'format': 'json', 'utf8': '',
				                        'lgname': settings["wiki_bot_login"],
				                        'lgpassword': settings["wiki_bot_password"],
				                        'lgtoken': response.json()['query']['tokens']['logintoken']}))
		except ValueError:
			logger.error("Logging in have not succeeded")
			return
		except MWError:
			logger.error("Logging in have not succeeded")
			return
		try:
			if response.json()['login']['result'] == "Success":
				logger.info("Successfully logged in")
				self.logged_in = True
			else:
				logger.error("Logging in have not succeeded")
		except:
			logger.error("Logging in have not succeeded")

	def fetch(self, amount=settings["limit"]):
		messagequeue.resend_msgs()
		last_check = self.fetch_changes(amount=amount)
		if last_check is not None:
			storage["rcid"] = last_check[0] if last_check[0] else storage["rcid"]
			storage["abuse_log_id"] = last_check[1] if last_check[1] else storage["abuse_log_id"]
			storage.save_datafile()
		self.initial_run_complete = True

	def fetch_recentchanges_request(self, amount):
		"""Make a typical MW request for rc/abuselog

		If succeeds return the .json() of request and if not raises ConnectionError"""
		request = self._safe_request(WIKI_API_PATH, params=self.construct_params(amount))
		if request is not None:
			try:
				request = request.json()
			except ValueError:
				logger.warning("ValueError in fetching changes")
				logger.warning("Changes URL:" + request.url)
				self.downtime_controller(True)
				raise ConnectionError
			return request
		raise ConnectionError

	def construct_params(self, amount):
		"""Constructs GET parameters for recentchanges/abuselog fetching feature"""
		params = OrderedDict(action="query", format="json")
		params["list"] = "recentchanges|abuselog" if settings.get("show_abuselog", False) else "recentchanges"
		params["rcshow"] = "" if settings.get("show_bots", False) else "!bot"
		params["rcprop"] = "title|redirect|timestamp|ids|loginfo|parsedcomment|sizes|flags|tags|user"
		params["rclimit"] = amount
		params["rctype"] = "edit|new|log|external|categorize" if settings.get("show_added_categories", True) else "edit|new|log|external"
		if settings.get("show_abuselog", False):
			params["afllimit"] = amount
			params["aflprop"] = "ids|user|title|action|result|timestamp|hidden|revid|filter"
		return params

	def prepare_rc(self, changes: list, amount: int):
		"""Processes recent changes messages"""
		if not changes:
			return None
		categorize_events = {}
		new_events = 0
		changes.reverse()
		highest_id = recent_id = storage["rcid"]
		dry_run = True if recent_id is None else False
		for change in changes:
			if not dry_run and not (change["rcid"] <= recent_id):
				new_events += 1
				logger.debug(
					"New event: {}".format(change["rcid"]))
				if new_events == settings["limit"] and not (amount == settings["limitrefetch"] and self.initial_run_complete is False):
					if amount < 500:
						# call the function again with max limit for more results, ignore the ones in this request
						logger.debug("There were too many new events, requesting max amount of events from the wiki.")
						return self.fetch(amount=5000 if self.logged_in else 500)
					else:
						logger.debug(
							"There were too many new events, but the limit was high enough we don't care anymore about fetching them all.")
			if change["type"] == "categorize":
				if "commenthidden" not in change:
					if len(wiki.mw_messages.keys()) > 0:
						cat_title = change["title"].split(':', 1)[1]
						# I so much hate this, blame Markus for making me do this
						if change["revid"] not in categorize_events:
							categorize_events[change["revid"]] = {"new": set(), "removed": set()}
						comment_to_match = re.sub(r'<.*?a>', '', change["parsedcomment"])
						if wiki.mw_messages["recentchanges-page-added-to-category"] in comment_to_match or \
								wiki.mw_messages[
									"recentchanges-page-added-to-category-bundled"] in comment_to_match:
							categorize_events[change["revid"]]["new"].add(cat_title)
							logger.debug("Matched {} to added category for {}".format(cat_title, change["revid"]))
						elif wiki.mw_messages[
							"recentchanges-page-removed-from-category"] in comment_to_match or \
								wiki.mw_messages[
									"recentchanges-page-removed-from-category-bundled"] in comment_to_match:
							categorize_events[change["revid"]]["removed"].add(cat_title)
							logger.debug("Matched {} to removed category for {}".format(cat_title, change["revid"]))
						else:
							logger.debug(
								"Unknown match for category change with messages {}, {}, {}, {} and comment_to_match {}".format(
									wiki.mw_messages["recentchanges-page-added-to-category"],
									wiki.mw_messages["recentchanges-page-removed-from-category"],
									wiki.mw_messages["recentchanges-page-removed-from-category-bundled"],
									wiki.mw_messages["recentchanges-page-added-to-category-bundled"],
									comment_to_match))
					else:
						logger.warning(
							"Init information not available, could not read category information. Please restart the bot.")
				else:
					logger.debug("Log entry got suppressed, ignoring entry.")
			if highest_id is None or change["rcid"] > highest_id:
				highest_id = change["rcid"]
		if not dry_run:
			for change in changes:
				if change["rcid"] <= recent_id:
					logger.debug("Change ({}) is lower or equal to recent_id {}".format(change["rcid"], recent_id))
					continue
				logger.debug(recent_id)
				rc_processor(change, categorize_events.get(change.get("revid"), None))
		return highest_id

	def prepare_abuse_log(self, abuse_log: list):
		if not abuse_log:
			return None
		abuse_log.reverse()
		recent_id = storage["abuse_log_id"]
		dryrun = True if recent_id is None else False
		for entry in abuse_log:
			if dryrun:
				continue
			if entry["id"] <= recent_id:
				continue
			abuselog_processing(entry, self)
		return entry["id"]

	def fetch_changes(self, amount):
		"""Fetches the :amount: of changes from the wiki.
		Returns None on error and int of rcid of latest change if succeeded"""
		global logged_in
		rc_last_id = None
		abuselog_last_id = None
		try:
			request_json = self.fetch_recentchanges_request(amount)
		except ConnectionError:
			return
		try:
			rc = request_json["query"]['recentchanges']
		except KeyError:
			logger.warning("Path query.recentchanges not found inside request body. Skipping...")
			return
		else:
			self.downtime_controller(False)
			rc_last_id = self.prepare_rc(rc, amount)
		if settings.get("show_abuselog", False):
			try:
				abuselog = request_json["query"]["abuselog"]  # While LYBL approach would be more performant when abuselog is not in request body, I prefer this approach for its clarity
			except KeyError:
				if "warnings" in request_json:
					warnings = request_json.get("warnings", {"query": {"*": ""}})
					if "Unrecognized value for parameter \"list\": abuselog." in warnings["query"]["*"]:
						settings["show_abuselog"] = False
						logger.warning("AbuseLog extension is not enabled on the wiki. Disabling the function...")
			else:
				abuselog_last_id = self.prepare_abuse_log(abuselog)
		return rc_last_id, abuselog_last_id

	def _safe_request(self, url, params=None):
		"""This method is depreciated, please use api_request"""
		logger.warning("safe_request is depreciated, please use api_request or own requests request")
		try:
			if params:
				request = self.session.get(url, params=params, timeout=10, allow_redirects=False)
			else:
				request = self.session.get(url, timeout=10, allow_redirects=False)
		except requests.exceptions.Timeout:
			logger.warning("Reached timeout error for request on link {url}".format(url=url))
			self.downtime_controller(True)
			return None
		except requests.exceptions.ConnectionError:
			logger.warning("Reached connection error for request on link {url}".format(url=url))
			self.downtime_controller(True)
			return None
		except requests.exceptions.ChunkedEncodingError:
			logger.warning("Detected faulty response from the web server for request on link {url}".format(url=url))
			self.downtime_controller(True)
			return None
		else:
			if 499 < request.status_code < 600:
				self.downtime_controller(True)
				return None
			elif request.status_code == 302:
				logger.critical("Redirect detected! Either the wiki given in the script settings (wiki field) is incorrect/the wiki got removed or is giving us the false value. Please provide the real URL to the wiki, current URL redirects to {}".format(request.next.url))
				sys.exit(0)
			return request

	def api_request(self, params: Union[str, OrderedDict], *json_path: list[str], timeout: int=10, allow_redirects: bool=False):
		"""Method to GET request data from the wiki's API with error handling including recognition of MediaWiki errors.
		
		Parameters:
			
			params (str, OrderedDict): a string or collections.OrderedDict object containing query parameters
			json_path (str): *args taking strings as values. After request is parsed as json it will extract data from given json path
			timeout (int, float) (default=10): int or float limiting time required for receiving a full response from a server before returning TimeoutError
			allow_redirects (bool) (default=False): switches whether the request should follow redirects or not
			
		Returns:
			
			request_content (dict): a dict resulting from json extraction of HTTP GET request with given json_path
			OR
			One of the following exceptions:
			ServerError: When connection with the wiki failed due to server error
			ClientError: When connection with the wiki failed due to client error
			KeyError: When json_path contained keys that weren't found in response JSON response
			BadRequest: When params argument is of wrong type
			MediaWikiError: When MediaWiki returns an error
		"""
		# Making request
		try:
			if isinstance(params, str):
				request = self.session.get(WIKI_API_PATH + params, timeout=timeout, allow_redirects=allow_redirects)
			elif isinstance(params, OrderedDict):
				request = self.session.get(WIKI_API_PATH, params=params, timeout=timeout, allow_redirects=allow_redirects)
			else:
				raise BadRequest(params)
		except requests.exceptions.Timeout:
			logger.warning("Reached timeout error for request on link {url}".format(url=WIKI_API_PATH+str(params)))
			self.downtime_controller(True)
			raise ServerError
		except requests.exceptions.ConnectionError:
			logger.warning("Reached connection error for request on link {url}".format(url=WIKI_API_PATH+str(params)))
			self.downtime_controller(True)
			raise ServerError
		except requests.exceptions.ChunkedEncodingError:
			logger.warning("Detected faulty response from the web server for request on link {url}".format(url=WIKI_API_PATH+str(params)))
			self.downtime_controller(True)
			raise ServerError
		# Catching HTTP errors
		if 499 < request.status_code < 600:
			self.downtime_controller(True)
			raise ServerError
		elif request.status_code == 302:
			logger.critical(
				"Redirect detected! Either the wiki given in the script settings (wiki field) is incorrect/the wiki got removed or is giving us the false value. Please provide the real URL to the wiki, current URL redirects to {}".format(
					request.next.url))
			sys.exit(0)
		elif 399 < request.status_code < 500:
			logger.error("Request returned ClientError status code on {url}".format(url=request.url))
			raise ClientError(request)
		else:
			# JSON Extraction
			try:
				request_json = parse_mw_request_info(request.json(), request.url)
				for item in request_json:
					request_json = request_json[item]
			except ValueError:
				logger.warning("ValueError when extracting JSON data on {url}".format(url=request.url))
				self.downtime_controller(True)
				raise ServerError
			except MediaWikiError:
				logger.exception("MediaWiki error on request: {}".format(request.url))
				raise
			except KeyError:
				raise
		return request_json

	def check_connection(self, looped=False):
		online = 0
		for website in ["https://google.com", "https://instagram.com", "https://steamcommunity.com"]:
			try:
				requests.get(website, timeout=10)
				online += 1
			except requests.exceptions.ConnectionError:
				pass
			except requests.exceptions.Timeout:
				pass
		if online < 1:
			logger.error("Failure when checking Internet connection at {time}".format(
				time=time.strftime("%a, %d %b %Y %H:%M:%S", time.localtime())))
			self.downtimecredibility = 0
			if not looped:
				while 1:  # recursed loop, check for connection (every 10 seconds) as long as three services are down, don't do anything else
					if self.check_connection(looped=True):
						wiki.fetch(amount=settings["limitrefetch"])
						break
					time.sleep(10)
			return False
		return True

	def downtime_controller(self, down):
		if not settings["show_updown_messages"]:
			return
		if down:
			if self.streak > -1:  # reset the streak of successful connections when bad one happens
				self.streak = 0
			if self.downtimecredibility < 60:
				self.downtimecredibility += 15
			else:
				if (
						time.time() - self.last_downtime) > 1800 and self.check_connection():  # check if last downtime happened within 30 minutes, if yes, don't send a message
					send_simple("down_detector", _("{wiki} seems to be down or unreachable.").format(wiki=settings["wikiname"]),
					     _("Connection status"), settings["avatars"]["connection_failed"])
					self.last_downtime = time.time()
					self.streak = 0
		else:
			if self.downtimecredibility > 0:
				self.downtimecredibility -= 1
				if self.streak > -1:
					self.streak += 1
				if self.streak > 8:
					self.streak = -1
					send_simple("down_detector", _("Connection to {wiki} seems to be stable now.").format(
						wiki=settings["wikiname"]),
					            _("Connection status"), settings["avatars"]["connection_restored"])

	def clear_cache(self):
		self.map_ips = {}
		if AUTO_SUPPRESSION_ENABLED:
			from src.fileio.database import clean_entries
			clean_entries()

	def init_info(self):
		startup_info = safe_read(self._safe_request(
			"{wiki}?action=query&format=json&uselang=content&list=tags&meta=allmessages%7Csiteinfo&utf8=1&tglimit=max&tgprop=displayname&ammessages=recentchanges-page-added-to-category%7Crecentchanges-page-removed-from-category%7Crecentchanges-page-added-to-category-bundled%7Crecentchanges-page-removed-from-category-bundled&amenableparser=1&amincludelocal=1&siprop=namespaces".format(
				wiki=WIKI_API_PATH)), "query")
		if startup_info:
			if "tags" in startup_info and "allmessages" in startup_info:
				for tag in startup_info["tags"]:
					try:
						self.tags[tag["name"]] = (BeautifulSoup(tag["displayname"], "lxml")).get_text()
					except KeyError:
						self.tags[tag["name"]] = None  # Tags with no display name are hidden and should not appear on RC as well
				for message in startup_info["allmessages"]:
					if not "missing" in message:  # ignore missing strings
						self.mw_messages[message["name"]] = message["*"]
					else:
						logging.warning("Could not fetch the MW message translation for: {}".format(message["name"]))
				for key, message in self.mw_messages.items():
					if key.startswith("recentchanges-page-"):
						self.mw_messages[key] = re.sub(r'\[\[.*?\]\]', '', message)
				self.namespaces = startup_info["namespaces"]
				logger.info("Gathered information about the tags and interface messages.")
			else:
				logger.warning("Could not retrieve initial wiki information. Some features may not work correctly!")
				logger.debug(startup_info)
		else:
			logger.error("Could not retrieve initial wiki information. Possibly internet connection issue?")

	def pull_comment(self, comment_id):
		try:
			comment = self.handle_mw_errors(self._safe_request(
				"{wiki}?action=comment&do=getRaw&comment_id={comment}&format=json".format(wiki=WIKI_API_PATH,
				                                                                          comment=comment_id)).json())[
				"text"]
			logger.debug("Got the following comment from the API: {}".format(comment))
		except MWError:
			pass
		except (TypeError, AttributeError):
			logger.exception("Could not resolve the comment text.")
		except KeyError:
			logger.exception("CurseProfile extension API did not respond with a valid comment content.")
		else:
			if len(comment) > 1000:
				comment = comment[0:1000] + "…"
			return comment
		return ""


wiki = Wiki()


def rc_processor(change, changed_categories):
	"""Prepares essential information for both embed and compact message format."""
	formatters = src.api.client.client.get_formatters()  # TODO Make it better? Importing might be a hell
	logger.debug(change)
	context = Context(settings["appearance"]["mode"], settings["webhookURL"], src.api.client.client)
	if ("actionhidden" in change or "suppressed" in change) and "suppressed" not in settings["ignored"]:  # if event is hidden using suppression
		context.event = "suppressed"
	if "commenthidden" not in change:
		LinkParser.feed(change["parsedcomment"])
		parsed_comment = LinkParser.new_string
		LinkParser.new_string = ""
		parsed_comment = re.sub(r"(`|_|\*|~|{|}|\|\|)", "\\\\\\1", parsed_comment, 0)
	else:
		parsed_comment = _("~~hidden~~")
	if not parsed_comment:
		parsed_comment = None
	if "userhidden" in change:
		change["user"] = _("hidden")
	if change.get("ns", -1) in settings.get("ignored_namespaces", ()):
		return
	if change["type"] in ["edit", "new"]:
		logger.debug("List of categories in essential_info: {}".format(changed_categories))
		identification_string = change["type"]
	elif change["type"] == "log":
		identification_string = "{logtype}/{logaction}".format(logtype=change["logtype"], logaction=change["logaction"])
		if identification_string not in supported_logs:
			logger.warning(
				"This event is not implemented in the script. Please make an issue on the tracker attaching the following info: wiki url, time, and this information: {}".format(
					change))
			return
	elif change["type"] == "categorize":
		return
	else:
		logger.warning("This event is not implemented in the script. Please make an issue on the tracker attaching the following info: wiki url, time, and this information: {}".format(change))
		return
	if identification_string in settings["ignored"]:
		return
	appearance_mode(identification_string, change, parsed_comment, changed_categories, wiki)

def abuselog_processing(entry, recent_changes):
	abuselog_appearance_mode(entry, recent_changes)