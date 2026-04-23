import logging
import os
import pickle
import requests

from contextlib import suppress
from pickle import UnpicklingError
from typing import Any

from authlib.common.security import generate_token
from authlib.integrations.base_client import TokenExpiredError, InvalidTokenError
from authlib.integrations.requests_client import OAuth2Session

from .PyViCareUtils import (PyViCareInvalidConfigurationError, PyViCareInvalidCredentialsError, PyViCareRateLimitError, PyViCareInternalServerError, PyViCareCommandError)

logger = logging.getLogger('ViCare')
logger.addHandler(logging.NullHandler())

API_BASE_URL = 'https://api.viessmann-climatesolutions.com/iot/v2'

VIESSMANN_SCOPE = ["Internal openid offline_access"]
AUTHORIZE_URL = 'https://iam.viessmann-climatesolutions.com/idp/v3/authorize'
TOKEN_URL = 'https://iam.viessmann-climatesolutions.com/idp/v3/token'
REDIRECT_URI = "vicare://oauth-callback/everest"


class ViCareOAuthManager():


    def __init__(self, username, password, client_id, token_file):
        self.username = username
        self.password = password
        self.token_file = token_file
        self.client_id = client_id
        oauth_session = self.__restore_oauth_session_from_token(token_file)
        self.__oauth = oauth_session

    @property
    def oauth_session(self) -> OAuth2Session:
        return self.__oauth

    def replace_session(self, new_session: OAuth2Session) -> None:
        self.__oauth = new_session

    def get(self, url: str) -> Any:
        try:
            logger.debug(self.__oauth)
            response = self.__oauth.get(f"{API_BASE_URL}{url}", timeout=31).json()
            logger.debug("Response to get request: %s", response)
            self.__handle_expired_token(response)
            self.__handle_rate_limit(response)
            self.__handle_server_error(response)
            return response
        except TokenExpiredError:
            self.renewToken()
            return self.get(url)
        except InvalidTokenError:
            self.renewToken()
            return self.get(url)

    def __restore_oauth_session_from_token(self, token_file):
        existing_token = self.__deserialize_token(token_file)
        if existing_token is not None:
            return OAuth2Session(self.client_id, token=existing_token)

        return self.__create_new_session(self.username, self.password, token_file)

    def __create_new_session(self, username, password, token_file=None):
        """Create a new oAuth2 sessions
        Viessmann tokens expire after 3600s (60min)
        Parameters
        ----------
        username : str
            e-mail address
        password : str
            password
        token_file: str
            path to serialize the token (will restore if already existing). No serialisation if not present

        Returns
        -------
        oauth:
            oauth sessions object
        """
        oauth_session = OAuth2Session(self.client_id, redirect_uri=REDIRECT_URI, scope=VIESSMANN_SCOPE, code_challenge_method='S256')
        code_verifier = generate_token(48)
        authorization_url, _ = oauth_session.create_authorization_url(AUTHORIZE_URL, code_verifier=code_verifier)
        logger.debug("Auth URL is: %s", authorization_url)

        header = {'Content-Type': 'application/x-www-form-urlencoded'}
        response = requests.post(authorization_url, headers=header, auth=(username, password), allow_redirects=False, timeout=30)

        if response.status_code == 401:
            raise PyViCareInvalidConfigurationError(response.json())

        if 'Location' not in response.headers:
            logger.debug('Response: %s', response)
            raise PyViCareInvalidCredentialsError()

        oauth_session.fetch_token(TOKEN_URL, authorization_response=response.headers['Location'], code_verifier=code_verifier, timeout=30)

        if oauth_session.token is None:
            raise PyViCareInvalidCredentialsError()

        logger.debug("Token received: %s", oauth_session.token)
        self.__serialize_token(oauth_session.token, token_file)
        logger.info("New token created")
        return oauth_session

    def renewToken(self):
        logger.info("Token expired, renewing")
        self.replace_session(self.__create_new_session(self.username, self.password, self.token_file))
        logger.info("Token renewed successfully")

    def getMe(self):
        logger.info(self.__oauth.get("https://api.viessmann.com/users/v1/users/me").json())

    def __serialize_token(self, oauth, token_file):
        logger.debug("Start serial")
        if token_file is None:
            logger.debug("Skip serial, no file provided.")
            return

        with open(token_file, mode='wb') as binary_file:
            pickle.dump(oauth, binary_file)

        logger.info("Token serialized to %s", token_file)

    def __deserialize_token(self, token_file):
        if token_file is None or not os.path.isfile(token_file):
            logger.debug("Token file argument not provided or file does not exist")
            return None

        logger.info("Token file exists")
        with suppress(UnpicklingError):
            with open(token_file, mode='rb') as binary_file:
                s_token = pickle.load(binary_file)
                logger.info("Token restored from file")
                return s_token
        logger.warning("Could not restore token")
        return None

    def __handle_expired_token(self, response):
        if ("error" in response and response["error"] == "EXPIRED TOKEN"):
            raise TokenExpiredError(response)

    def __handle_rate_limit(self, response):
        if ("statusCode" in response and response["statusCode"] == 429):
            raise PyViCareRateLimitError(response)

    def __handle_server_error(self, response):
        if ("statusCode" in response and response["statusCode"] >= 500):
            raise PyViCareInternalServerError(response)

    def __handle_command_error(self, response):
        if ("statusCode" in response and response["statusCode"] >= 400):
            raise PyViCareCommandError(response)

    def post(self, url, data) -> Any:
        """POST URL using OAuth session. Automatically renew the token if needed
            Parameters
            ----------
            url : str
                URL to get
            data : str
                Data to post

            Returns
            -------
            result: json
                json representation of the answer
            """
        self.post_raw(f"{API_BASE_URL}{url}", data)

    def post_raw(self, url, data) -> Any:
        headers = {"Content-Type": "application/json", "Accept": "application/vnd.siren+json"}
        try:
            response = self.__oauth.post(f"{url}", data, headers=headers).json()
            self.__handle_expired_token(response)
            self.__handle_rate_limit(response)
            self.__handle_command_error(response)
            return response
        except TokenExpiredError:
            self.renewToken()
            return self.post(url, data)
        except InvalidTokenError:
            self.renewToken()
            return self.get(url)
