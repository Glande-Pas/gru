#!/usr/bin/env python3

import json
import os
import pathlib
import datetime
import threading
import secrets
import requests
from http.server import BaseHTTPRequestHandler, HTTPServer
import webbrowser
from urllib import parse as urlparse, request as urlrequest


def _env():
    """Read KEY=VALUE pairs from a local, gitignored .env file."""
    try:
        lines = (pathlib.Path(__file__).parent / '.env').read_text().splitlines()
    except OSError:
        return {}
    return dict(l.split('=', 1) for l in lines if '=' in l and not l.startswith('#'))


class ESOLogsOAuth:
    hostname = 'www.esologs.com'
    client_id = '9ecd0323-19e3-4a29-a8e6-72438a80bcbf'
    redirect_uri = 'http://localhost:8000/oauth-callback'
    client_secret = os.environ.get('ESOLOGS_CLIENT_SECRET') or _env().get('ESOLOGS_CLIENT_SECRET', '')

    cache: pathlib.Path = pathlib.Path(__file__).parent / 'cache' / 'token.json'

    @classmethod
    def url(cls, path, query_dict = {}):
        return urlparse.urlunsplit(('https', cls.hostname, path, urlparse.urlencode(query_dict), ''))


    @classmethod
    def webflow_authorization(cls):
        if os.environ.get('ESOLOGS_MANUAL'):
            return cls.manual_authorization()
        callback_params = {}

        class AuthorizationHandler(BaseHTTPRequestHandler):
            """
            Simple HTTP request handler to capture the authorization code
            From https://stackoverflow.com/questions/76783429
            """
            def do_GET(self):
                if not self.path.startswith('/oauth-callback?'):
                    return

                path, query = self.path.split('?', 1)

                callback_params.clear()
                callback_params.update(dict(urlparse.parse_qsl(query)))

                # Send a response to the browser
                self.send_response(200)
                self.send_header('Content-type', 'text/html')
                self.end_headers()
                self.wfile.write(b'''<h1>ESOLogs Authorization Code Received</h1><p>You can safely close this window.</p>''')

                # Satisfied with result, exit thread
                raise SystemExit

        # Startup the local server to wait for callback info
        server = HTTPServer(('localhost', 8000), AuthorizationHandler)
        server_thread = threading.Thread(target=server.serve_forever)
        server_thread.start()

        # Open the authorization URL in the default web browser
        state = secrets.token_urlsafe(128)
        authorization_url = cls.url('/oauth/authorize', {
            'response_type': 'code',
            'redirect_uri': cls.redirect_uri,
            'client_id': cls.client_id,
            'state': state,
        })
        webbrowser.open(authorization_url)

        try:
            server_thread.join()
        except:
            # KeyboardInterrupt or something else, tell server thread to exit
            server.shutdown()
            raise
        finally:
            server.server_close()

        if callback_params.get('state') != state:
            raise ValueError('Incorrect state returned, MITM attack prevented?')

        return callback_params['code']


    @classmethod
    def manual_authorization(cls):
        """For remote/headless runs: user opens the URL, then pastes the (failed) localhost redirect URL."""
        state = secrets.token_urlsafe(32)
        print('Open this URL, log in, authorize:\n\n' + cls.url('/oauth/authorize', {
            'response_type': 'code',
            'redirect_uri': cls.redirect_uri,
            'client_id': cls.client_id,
            'state': state,
        }))
        redirected = input('\nPaste the full URL you were redirected to: ').strip()
        params = dict(urlparse.parse_qsl(urlparse.urlsplit(redirected).query))
        if params.get('state') != state:
            raise ValueError('State mismatch')
        return params['code']


    @classmethod
    def fetch_token(cls, grant_type: str, code: str):
        """ Get a new token from a given source of granting us authorization """
        now = datetime.datetime.now(datetime.UTC)

        # Obtain OAuth Client Access Token
        response = requests.post(cls.url('/oauth/token'), headers={'accept': '*/*'}, data={
            'redirect_uri': cls.redirect_uri,
            'grant_type': grant_type,
            ('refresh_token' if grant_type == 'refresh_token' else 'code'): code,
        }, auth=requests.auth.HTTPBasicAuth(cls.client_id, cls.client_secret))

        response.raise_for_status()

        data = response.json()
        data['expiration'] = now.timestamp() + data.pop('expires_in')

        with cls.cache.parent.mkdir(exist_ok=True) or cls.cache.open('w') as f:
            json.dump(data, f)

        return data


    @classmethod
    def cached(cls):
        try:
            with cls.cache.open() as f:
                return json.load(f)
        except:
            return {'expiration': 0}


    @classmethod
    def auth_header(cls):
        token = cls.cached()
        now = datetime.datetime.now(datetime.UTC)

        # Typically 360 days validity on tokens so untested whether you can refresh token after validity ??
        if token['expiration'] < now.timestamp() and 'refresh_token' in token:
            try:
                token = cls.fetch_token('refresh_token', token['refresh_token'])
            except:
                pass  # Fall back to web auth flow

        if token['expiration'] < now.timestamp():
            token = cls.fetch_token('authorization_code', cls.webflow_authorization())

        return '{token_type} {access_token}'.format(**token)


if __name__ == '__main__':
    print(ESOLogsOAuth.auth_header())
