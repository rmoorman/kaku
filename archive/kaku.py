#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
:copyright: (c) 2015-2016 by Mike Taylor
:license: MIT, see LICENSE for more details.

A Flask service to handle the more dynamic HTML
events that IndieWeb requires and would otherwise
require javascript somewhere on the site.
"""

import os
import sys
import uuid
import json
import urllib
import logging

import ninka
import redis
import requests

from logging.handlers import RotatingFileHandler
from urlparse import ParseResult, urlparse
from bearlib.config import Config
from bearlib.tools import baseDomain
from flask import Flask, request, redirect, render_template, session, jsonify, Response
from flask.ext.wtf import Form
from wtforms import TextField, HiddenField
from wtforms.validators import Required


class LoginForm(Form):
    me           = TextField('me', validators=[ Required() ])
    client_id    = HiddenField('client_id')
    redirect_uri = HiddenField('redirect_uri')
    from_uri     = HiddenField('from_uri')

class PubishForm(Form):
    h            = TextField('h', validators=[])
    content      = TextField('content', validators=[])
    title        = TextField('title', validators=[])
    published    = TextField('published', validators=[])
    inreplyto    = TextField('in-reply-to', validators=[])
    syndicateto  = TextField('syndicate-to', validators=[])

class TokenForm(Form):
    code         = TextField('code', validators=[])
    me           = TextField('me', validators=[])
    redirect_uri = TextField('redirect_uri', validators=[])
    client_id    = TextField('client_id', validators=[])
    state        = TextField('state', validators=[])

# check for uwsgi, use PWD if present or getcwd() if not
_uwsgi = __name__.startswith('uwsgi') or 'UWSGI_ORIGINAL_PROC_NAME' in os.environ.keys()
if _uwsgi:
    _ourPath = os.path.dirname(__name__.replace('uwsgi_file_', '').replace('_', '/'))
else:
    _ourPath = os.getcwd()
_configFile = os.path.join(_ourPath, 'kaku.cfg')

# uwsgi apps do not have their current working directory set (that I know of)
# to anything anywhere near the application so these are done at this point
# instead of at top to allow the path to be inserted into the import path list.
from handlers.webmention import mention
from handlers.micropub import micropub

app = Flask(__name__)
cfg = None
db  = None
app.config['SECRET_KEY'] = 'foo'  # replaced downstream

def clearAuth():
    if 'indieauth_token' in session:
        if db is not None:
            key = db.get('token-%s' % session['indieauth_token'])
            if key:
                db.delete(key)
                db.delete('token-%s' % session['indieauth_token'])
    session.pop('indieauth_token', None)
    session.pop('indieauth_scope', None)
    session.pop('indieauth_id',    None)

def checkAuth():
    authed       = False
    indieauth_id = None
    if 'indieauth_id' in session and 'indieauth_token' in session:
        app.logger.info('session cookie found')
        indieauth_id    = session['indieauth_id']
        indieauth_token = session['indieauth_token']
        if db is not None:
            key = db.get('token-%s' % indieauth_token)
            if key:
                data = db.hgetall(key)
                if data and data['token'] == indieauth_token:
                    authed = True
    return authed, indieauth_id

def checkAccessToken(access_token):
    if access_token is not None and db is not None:
        key = db.get('token-%s' % access_token)
        if key:
            data      = key.split('-')
            me        = data[1]
            client_id = data[2]
            scope     = data[3]
            app.logger.info('access token valid [%s] [%s] [%s]' % (me, client_id, scope))
            return me, client_id, scope
    else:
        return None, None, None

@app.route('/logout', methods=['GET'])
def handleLogout():
    app.logger.info('handleLogout [%s]' % request.method)
    clearAuth()
    return redirect('/')

@app.route('/login', methods=['GET', 'POST'])
def handleLogin():
    app.logger.info('handleLogin [%s]' % request.method)

    me          = None
    redirectURI = '%s/success' % cfg.baseurl
    fromURI     = request.args.get('from_uri')

    app.logger.info('redirectURI [%s] fromURI [%s]' % (redirectURI, fromURI))
    form = LoginForm(me='',
                     client_id=cfg.client_id,
                     redirect_uri=redirectURI,
                     from_uri=fromURI)

    if form.validate_on_submit():
        app.logger.info('me [%s]' % form.me.data)

        me            = 'https://%s/' % baseDomain(form.me.data, includeScheme=False)
        authEndpoints = ninka.indieauth.discoverAuthEndpoints(me)

        if 'authorization_endpoint' in authEndpoints:
            authURL = None
            for url in authEndpoints['authorization_endpoint']:
                authURL = url
                break
            if authURL is not None:
                url = ParseResult(authURL.scheme,
                                  authURL.netloc,
                                  authURL.path,
                                  authURL.params,
                                  urllib.urlencode({ 'me':            me,
                                                     'redirect_uri':  form.redirect_uri.data,
                                                     'client_id':     form.client_id.data,
                                                     'scope':         'post',
                                                     'response_type': 'id'
                                                   }),
                                  authURL.fragment).geturl()
                if db is not None:
                    key  = 'login-%s' % me
                    data = db.hgetall(key)
                    if data and 'token' in data:  # clear any existing auth data
                        db.delete('token-%s' % data['token'])
                        db.hdel(key, 'token')
                    db.hset(key, 'auth_url',     ParseResult(authURL.scheme, authURL.netloc, authURL.path, '', '', '').geturl())
                    db.hset(key, 'from_uri',     form.from_uri.data)
                    db.hset(key, 'redirect_uri', form.redirect_uri.data)
                    db.hset(key, 'client_id',    form.client_id.data)
                    db.hset(key, 'scope',        'post')
                    db.expire(key, cfg.auth_timeout)  # expire in N minutes unless successful
                app.logger.info('redirecting to [%s]' % url)
                return redirect(url)
        else:
            return 'insert fancy no auth endpoint found error message here', 403

    templateContext = {}
    templateContext['title'] = 'Sign In'
    templateContext['form']  = form
    return render_template('login.jinja', **templateContext)

@app.route('/success', methods=['GET', ])
def handleLoginSuccess():
    app.logger.info('handleLoginSuccess [%s]' % request.method)
    scope = None
    me    = request.args.get('me')
    code  = request.args.get('code')
    app.logger.info('me [%s] code [%s]' % (me, code))

    if db is not None:
        app.logger.info('getting data to validate auth code')
        key  = 'login-%s' % me
        data = db.hgetall(key)
        if data:
            app.logger.info('calling [%s] to validate code' % data['auth_url'])
            r = ninka.indieauth.validateAuthCode(code=code,
                                                 client_id=data['client_id'],
                                                 redirect_uri=data['redirect_uri'],
                                                 validationEndpoint=data['auth_url'])
            if r['status'] == requests.codes.ok:
                app.logger.info('login code verified')
                if 'scope' in r['response']:
                    scope = r['response']['scope']
                else:
                    scope = data['scope']
                from_uri = data['from_uri']
                token    = str(uuid.uuid4())

                db.hset(key, 'code',  code)
                db.hset(key, 'token', token)
                db.expire(key, cfg['auth_timeout'])
                db.set('token-%s' % token, key)
                db.expire('token-%s' % code, cfg['auth_timeout'])

                session['indieauth_token'] = token
                session['indieauth_scope'] = scope
                session['indieauth_id']    = me
            else:
                app.logger.info('login invalid')
                clearAuth()
        else:
            app.logger.info('nothing found for [%s]' % me)

    if scope:
        if from_uri:
            return redirect(from_uri)
        else:
            return redirect('/')
    else:
        return 'authentication failed', 403

@app.route('/auth', methods=['GET', ])
def handleAuth():
    app.logger.info('handleAuth [%s]' % request.method)
    result = False
    if db is not None:
        token = request.args.get('token')
        if token is not None:
            me = db.get('token-%s' % token)
            if me:
                data = db.hgetall(me)
                if data and data['token'] == token:
                    result = True
    if result:
        return 'valid', 200
    else:
        clearAuth()
        return 'invalid', 403

@app.route('/micropub', methods=['GET', 'POST', 'PATCH', 'PUT', 'DELETE'])
def handleMicroPub():
    app.logger.info('handleMicroPub [%s]' % request.method)
    # form = MicroPubForm()

    access_token = request.headers.get('Authorization')
    if access_token:
        access_token = access_token.replace('Bearer ', '')
    me, client_id, scope = checkAccessToken(access_token)
    app.logger.info('[%s] [%s] [%s] [%s]' % (access_token, me, client_id, scope))

    if me is None or client_id is None:
        return ('Access Token missing', 401, {})
    else:
        if request.method == 'POST':
                domain   = baseDomain(me, includeScheme=False)
                idDomain = baseDomain(cfg.client_id, includeScheme=False)
                if domain == idDomain and checkAccessToken(access_token):
                    data = { 'event':     'create',
                             'domain':    domain,
                             'baseurl':   cfg.baseurl,
                             'baseroute': cfg.baseroute,
                             'app':       client_id,
                             'scope':     scope
                           }
                    for key in ('h', 'name', 'summary', 'content', 'published', 'updated',
                                'category', 'slug', 'location', 'syndication', 'syndicate-to',
                                'in-reply-to', 'repost-of', 'like-of'):
                        data[key] = request.form.get(key)
                        app.logger.info('    %s = [%s]' % (key, data[key]))
                    for key in request.form.keys():
                        if key not in data:
                            data[key] = request.form.get(key)
                            app.logger.info('    %s = [%s]' % (key, data[key]))
                    return micropub(data, db, app.logger, cfg.site_config)
                else:
                    return 'Unauthorized', 403
        elif request.method == 'GET':
            # add support for /micropub?q=syndicate-to
            return 'not implemented', 501

@app.route('/token', methods=['POST', 'GET'])
def handleToken():
    app.logger.info('handleToken [%s]' % request.method)

    if request.method == 'GET':
        access_token = request.headers.get('Authorization')
        if access_token:
            access_token = access_token.replace('Bearer ', '')
        else:
            access_token
        me, client_id, scope = checkAccessToken(access_token)

        if me is None or client_id is None:
            return ('Token is not valid', 400, {})
        else:
            params = { 'me':        me,
                       'client_id': client_id,
                     }
            if scope is not None:
                params['scope'] = scope
            return (urllib.urlencode(params), 200, {'Content-Type': 'application/x-www-form-urlencoded'})

    elif request.method == 'POST':
        code         = request.form.get('code')
        me           = request.form.get('me')
        redirect_uri = request.form.get('redirect_uri')
        client_id    = request.form.get('client_id')
        state        = request.form.get('state')

        app.logger.info('    code         [%s]' % code)
        app.logger.info('    me           [%s]' % me)
        app.logger.info('    client_id    [%s]' % client_id)
        app.logger.info('    state        [%s]' % state)
        app.logger.info('    redirect_uri [%s]' % redirect_uri)

        r = ninka.indieauth.validateAuthCode(code=code,
                                             client_id=me,
                                             state=state,
                                             redirect_uri=redirect_uri)
        if r['status'] == requests.codes.ok:
            app.logger.info('token request auth code verified')
            scope = r['response']['scope']
            key   = 'app-%s-%s-%s' % (me, client_id, scope)
            token = db.get(key)
            if token is None:
                token     = str(uuid.uuid4())
                token_key = 'token-%s' % token
                db.set(key, token)
                db.set(token_key, key)

            app.logger.info('  token generated for [%s] : [%s]' % (key, token))
            params = { 'me': me,
                       'scope': scope,
                       'access_token': token
                     }
            return (urllib.urlencode(params), 200, {'Content-Type': 'application/x-www-form-urlencoded'})

def validURL(targetURL):
    """Validate the target URL exists by making a HEAD request for it
    """
    result = 404
    try:
        r = requests.head(targetURL)
        result = r.status_code
    except:
        result = 404
    return result

@app.route('/webmention', methods=['POST'])
def handleWebmention():
    app.logger.info('handleWebmention [%s]' % request.method)
    if request.method == 'POST':
        valid  = False
        source = request.form.get('source')
        target = request.form.get('target')
        vouch  = request.form.get('vouch')
        app.logger.info('source: %s target: %s vouch %s' % (source, target, vouch))
        if cfg.baseroute in target:
            valid = validURL(target)
            app.logger.info('valid? %s' % valid)
            if valid == requests.codes.ok:
                valid, vouched = mention(source, target, db, app.logger, cfg.site_config, vouch, cfg.vouch_required)
                if valid:
                    return redirect(target)
                else:
                    if cfg.vouch_required and not vouched:
                        return 'Vouch required for webmention', 449
                    else:
                        return 'Webmention is invalid', 400
            else:
                return 'invalid post', 404
        else:
            return 'invalid post', 404

_xml_response = """<?xml version="1.0" encoding="utf-8" standalone="yes"?>
<oembed>
  <version>%(version)s</version>
  <type>%(type)s</type>
  <author_name>%(author_name)s</author_name>
  <author_url>%(author_url)s</author_url>
  <cache_age>86400</cache_age>
  <provider_name>%(provider_name)s</provider_name>
  <provider_url>%(provider_url)s</provider_url>
  <html>%(html)s</html>
</oembed>
"""
_embed_html = """<blockquote class="h-entry">
  <p>
    <a class="p-author h-card" href="%(author_url)s">
      <img src="%(thumbnail_url)s" height="%(thumbnail_height)s" width="%(thumbnail_width)s" alt="%(author_name)s"/>
    </a>
    <a class="u-url" href="%(target)s">%(title)s</a>
  </p>
</blockquote>
"""
_thumbnails = (
    ("/images/bear_apple-icon-57x57.png",     57),
    ("/images/bear_apple-icon-60x60.png",     60),
    ("/images/bear_apple-icon-72x72.png",     72),
    ("/images/bear_apple-icon-76x76.png",     76),
    ("/images/bear_apple-icon-114x114.png",   114),
    ("/images/bear_apple-icon-120x120.png",   120),
    ("/images/bear_128x128.jpg",              128),
    ("/images/bear_apple-icon-144x144.png",   144),
    ("/images/bear_apple-icon-152x152.png",   152),
    ("/images/bear_apple-icon-180x180.png",   180),
    ("/images/bear_android-icon-192x192.png", 192),
)

def findThumbnail(maxWidth, maxHeight):
    if maxWidth is None:
        maxWidth = 72
    if maxHeight is None:
        maxHeight = 72
    key    = min(maxWidth, maxHeight)
    result = _thumbnails[0]
    for u, v in _thumbnails:
        if v <= key:
            result = (u, v)
        else:
            break
    return result

@app.route('/oembed', methods=['GET'])
def handleEmbed():
    app.logger.info('handleEmbed')

    targetURL      = request.args.get('url')
    responseFormat = request.args.get('format')
    maxWidth       = request.args.get('maxwidth')
    maxHeight      = request.args.get('maxheight')
    if responseFormat is None:
        responseFormat = 'json'
    responseFormat = responseFormat.lower()

    if targetURL is None:
        return 'invalid url', 404
    else:
        siteCfg = Config()
        if os.path.exists(cfg.site_config):
            siteCfg.fromJson(cfg.site_config)

        url         = urlparse(targetURL)
        targetRoute = url.path.replace(siteCfg.baseroute, '')
        if targetRoute.endswith('.html'):
            targetRoute = targetRoute[:-5]
        targetFile = os.path.join(siteCfg.paths.content, '%s.json' % targetRoute)
        print targetURL
        print url
        print targetRoute
        print targetFile
        # load all known mentions for the target
        if os.path.exists(targetFile):
            with open(targetFile, 'r') as h:
                post = json.load(h)

        thumbUrl, thumbWidth = findThumbnail(maxWidth, maxHeight)
        data = {
            "version":          "1.0",
            "type":             "rich",
            "author_name":      post['author'],
            "author_url":       siteCfg.baseurl,
            "provider_name":    baseDomain(siteCfg.baseurl, includeScheme=False),
            "provider_url":     siteCfg.baseurl,
            "title":            post['title'],
            "thumbnail_url":    '%s%s' % (siteCfg.baseurl, thumbUrl),
            "thumbnail_width":  thumbWidth,
            "thumbnail_height": thumbWidth,
            "target":           targetURL
        }
        data['html'] = _embed_html % data

        if responseFormat == 'json':
            return jsonify(data)
        else:
            return Response(_xml_response % data, mimetype='text/xml')

def initLogging(logger, logpath=None, echo=False):
    logFormatter = logging.Formatter("%(asctime)s %(levelname)-9s %(message)s", "%Y-%m-%d %H:%M:%S")
    if logpath is not None:
        logfilename = os.path.join(logpath, 'kaku.log')
        logHandler  = RotatingFileHandler(logfilename, maxBytes=1024 * 1024 * 100, backupCount=7)
        logHandler.setFormatter(logFormatter)
        logger.addHandler(logHandler)
    if echo:
        echoHandler = logging.StreamHandler()
        echoHandler.setFormatter(logFormatter)
        logger.addHandler(echoHandler)
    logger.setLevel(logging.INFO)
    logger.info('starting kaku')

def loadConfig(configFilename, host=None, port=None, logpath=None):
    result = Config()
    result.fromJson(configFilename)
    if host is not None:
        result.host = host
    if port is not None:
        result.port = port
    if logpath is not None:
        result.paths.log = logpath
    if 'auth_timeout' not in result:
        result.auth_timeout = 300
    if 'require_vouch' not in result:
        result.require_vouch = False
    if 'our_domain' not in result:
        result.our_domain = baseDomain(result.client_id, includeScheme=False)
    return result

def getRedis(config):
    if 'host' not in config:
        config.host = '127.0.0.1'
    if 'port' not in config:
        config.port = 6379
    if 'db' not in config:
        config.db = 0
    return redis.StrictRedis(host=config.host, port=config.port, db=config.db)

def doStart(app, configFile, ourHost=None, ourPort=None, ourPath=None, echo=False):
    _cfg = loadConfig(configFile, host=ourHost, port=ourPort, logpath=ourPath)
    _db  = None
    if 'secret' in _cfg:
        app.config['SECRET_KEY'] = _cfg.secret
    initLogging(app.logger, _cfg.logpath, echo=echo)
    if 'redis' in _cfg:
        _db = getRedis(_cfg.redis)
    app.logger.info('configuration loaded from %s' % configFile)
    return _cfg, _db

if _uwsgi:
    cfg, db = doStart(app, _configFile)
#
# None of the below will be run for nginx + uwsgi
#
if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument('--host',     default='0.0.0.0')
    parser.add_argument('--port',     default=5000, type=int)
    parser.add_argument('--logpath',  default='.')
    parser.add_argument('--config',   default='./kaku.cfg')

    args = parser.parse_args()
    cfg, db = doStart(app, args.config, args.host, args.port, args.logpath, echo=True)

    app.run(host=cfg.host, port=cfg.port, debug=True)
