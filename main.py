#!/usr/bin/env python3

import re
import random
import string
import json
import yaml
import base64
import requests
import redis
import stripe
from authlib.integrations.starlette_client import OAuth
from starlette.requests import Request
from starlette.config import Config
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import HTMLResponse, RedirectResponse
from authlib.integrations.starlette_client import OAuth, OAuthError
from openai import AsyncOpenAI
from nicegui import context, app, ui

config = Config()
oauth = OAuth(config)
app.add_middleware(SessionMiddleware, secret_key=config('STORAGE_SECRET'))
aclient = AsyncOpenAI()

gpt_model = config('GPT_MODEL')
buy_link = config('BUY_LINK')
redis_host = config('REDIS_HOST')
redis_port = config('REDIS_PORT')
redis_password = config('REDIS_PASSWORD')
redis_db = redis.Redis(host=redis_host, port=redis_port, db=0, password=redis_password)
stripe.api_key = config('STRIPE_SECRET_KEY')
app.add_static_files('/static', 'static')

oauth.register(
    name='google',
    server_metadata_url=config('SERVER_METADATA_URL'),
    client_kwargs={'scope': 'email'}
)

with open('defaults.yml', 'r', encoding='utf-8') as file:
    defaults = yaml.safe_load(file)

patterns = [r'```mermaid([^`]+)```', r'(^timeline.+)', 
            r'(^sequenceDiagram.+)', r'(^stateDiagram-v2.+)', 
            r'(^graph.+)', r'(^flowchart.+)',
            r'(^gantt.+)',r'(^mindmap.+)',r'(^journey.+)',
            r'(^C4Context.+)'
            ]

def extract_text_from_patterns(text):
    extracted_text = []
    for pattern in patterns:
        matches = re.findall(pattern, text, re.DOTALL)
        extracted_text.extend(matches)
    return extracted_text[0]

def generate_random_string(length):
        letters = string.ascii_letters
        return ''.join(random.choice(letters) for _ in range(length))

def jscode_download(id):
    random_string = generate_random_string(10)
    return defaults['jsdecode'] % (id, random_string)

def store_data(user_id, key, data):
    if user_id:
        data = base64.b64encode(json.dumps(data).encode('utf-8'))
        redis_db.set(f"{user_id}:flowgenius:{key}", data)

def retrieve_data(user_id, key):
    if user_id:
        data = redis_db.get(f"{user_id}:flowgenius:{key}")
        if data:
            data = base64.b64decode(data)
            return json.loads(data.decode('utf-8'))
    return None

def mermaid_editor_link(mermaid_code):
    mermaid_editor_json = {
        "code": mermaid_code,
        "mermaid": {"theme": "default"},
        "updateEditor": False
    }
    mermaid_editor_json_string = json.dumps(mermaid_editor_json)
    buffer = base64.b64encode(mermaid_editor_json_string.encode()).decode()
    return f"https://mermaid.live/edit#{buffer}"

@app.get('/login')
async def login(request: Request):
    return await oauth.google.authorize_redirect(request, config('BASE_URL') + '/auth')

@app.get('/auth')
async def auth(request: Request):
    try:
        token = await oauth.google.authorize_access_token(request)
    except OAuthError as error:
        return HTMLResponse(f'<h1>{error.error}</h1>')
    access_token = token.get('access_token')
    response = requests.get(config('USER_INFO_ENDPOINT'), headers={'Authorization': f'Bearer {access_token}'})
    # Check if the request was successful
    if response.status_code == 200:
        request.session['user'] = response.json()
    else:
        return HTMLResponse('<h1>Failed to retrieve user information</h1>')
    return RedirectResponse(url='/')

@app.get('/logout')
def logout(request: Request):
    request.session.pop('user', None)
    user_id = None
    return RedirectResponse(url='/')

@ui.page('/about')
def about():
    with open('static/about.md', 'r') as file:
        ui.markdown(file.read())

@ui.page('/EULA')
def EULA():
    with open('static/EULA.md', 'r') as file:
        ui.markdown(file.read())

@app.post('/stripe_webhook')
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get('stripe-signature', None)
    event = None

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, config('STRIPE_ENDPOINT_SECRET'))
    except (ValueError, stripe.error.SignatureVerificationError) as e:
        print(f"Invalid payload {payload}")
        return HTMLResponse(status_code=400)

    if (event['type'] == 'checkout.session.completed' and 
        event['data']['object']['payment_status'] == 'paid'):
        user_id = event['data']['object']['metadata']['user_id']
        store_data(user_id, 'tokens', 10000)

@ui.page('/')
async def main(request: Request):
    user = request.session.get('user', None)
    user_id = user.get('id') if user else None
    user_messages = retrieve_data(user_id, 'messages') or defaults['welcome']
    graph_kinds = {i: i for i in defaults['patterns']}

    async def search(searchbar, spinner):
        tokens = retrieve_data(user_id, 'tokens') or defaults['min_tokens']
        searchbar.visible = False
        spinner.visible = True
        user_messages = await query_ia(searchbar.value, graph_kind=select_graph.value)
        store_data(user_id, 'messages', user_messages)
        searchbar.value = ''
        chat_messages.refresh()
        spinner.visible = False
        searchbar.visible = True
        searchbutton.visible = True
        
    async def query_ia(text, graph_kind) -> None:
        tries = 0
        if user_messages:
            user_messages.append(('You', text))
        ia_message_final = defaults['ia']
        ia_message_final[1]['content'] = defaults['content'] % graph_kind
        ia_message_final.append({'role': 'user', 'content': text})
        ia_message_final = [{'role': msg['role'], 'content': msg['content']} for msg in ia_message_final]
        
        while tries < defaults['max_tries']:
            response = await aclient.chat.completions.create(
                model=gpt_model,
                messages=ia_message_final,
                temperature=0.2,
                max_tokens=1000,
                top_p=1,
                frequency_penalty=0.0,
                presence_penalty=0.0)

            try:
                content = response.choices[0].message.content
                mermaid_code = extract_text_from_patterns(content)
                used_tokens = response.usage.total_tokens
                store_data(user_id, 'tokens', tokens-used_tokens)
                user_messages.append(('Chart', mermaid_code))
                tries = defaults['max_tries']
            except (TimeoutError,SyntaxError,IndexError):
                tries += 1
                if (tries >= defaults['max_tries']):
                    user_messages.append(('Failed', defaults['failed']))
        return user_messages

    def edit_card(id):
        link = mermaid_editor_link(user_messages[id][1])
        js_code = f"window.open('{link}', '_blank');"
        ui.run_javascript(js_code)

    async def show_fullscreen_overlay(id):
        svg = await ui.run_javascript(defaults['getsvg'] % id, timeout=10)
        with ui.dialog().props('maximized') as overlay, ui.card().classes('w-full h-full'):
            ui.html(svg).classes('w-full h-full overflow-auto p-4 bg-white')
            ui.button('✖', on_click=overlay.close).classes(
                'absolute top-2 right-2 text-xl text-black bg-transparent shadow-none')

        overlay.open()

    def delete_card(id):
        user_messages.pop(id)
        user_messages.pop(id - 1)
        store_data(user_id, 'messages', user_messages)
        chat_messages.refresh()

    def generate_buy_link():
        checkout_session = stripe.checkout.Session.create(
            line_items=[{'price': config('STRIPE_PRICE_ID'),'quantity': 1}],
            mode='payment',
            success_url=config('BASE_URL'),
            automatic_tax={'enabled': True},
            customer_email=user['email'],
            metadata={'user_id': user_id})
        return(checkout_session.url)

    # Header bar
    with ui.header().classes('items-center justify-between'):
        ui.markdown('FlowGenius').classes('text-xl')
        if user:
            with ui.button().props('unelevated'):
                ui.image(user['picture']).classes('rounded-full w-10 h-10')
                ui.icon('expand_more')
                with ui.menu():
                    tokens = retrieve_data(user_id, 'tokens')
                    if tokens == None: tokens = defaults['min_tokens']
                    ui.menu_item(f"{tokens} tokens")
                    select_graph = ui.select(graph_kinds, label="Graph kind")
                    with ui.menu_item().classes('p-0 m-0'):
                        ui.button('run', icon='send', on_click=lambda: search(searchbar, spinner)).classes('w-full')
                    with ui.menu_item().classes('p-0 m-0'):
                        ui.button('EULA', icon='policy', on_click=lambda: ui.navigate.to('/EULA')).classes('w-full')
                    with ui.menu_item().classes('p-0 m-0'):
                        ui.button('logout', icon='logout', on_click=lambda: ui.navigate.to('/logout')).classes('w-full')

    # Main content
    if not user:
        ui.image('static/authcode_flow.svg')

    # app interface
    @ui.refreshable
    async def chat_messages():
        await context.client.connected()
        if not user_messages:
            return

        # chat messages
        for i, [name, content] in enumerate(user_messages):
            if name=='Chart':
                with ui.card().tight() as card:
                    with ui.grid(columns=12):
                        with ui.button(on_click=lambda i=card.id: ui.run_javascript(jscode_download(i))) \
                                                                                    .classes('col-start-10'):
                            ui.icon('download')
                        with ui.button(on_click=lambda i=i: edit_card(i)).classes('col-start-11'):
                            ui.icon('edit')
                        with ui.button(on_click=lambda i=i: delete_card(i)).classes('col-start-12'):
                            ui.icon('delete_forever')
                    ui.mermaid(content).on('click', lambda i=card.id: show_fullscreen_overlay(i))
                    svg = await ui.run_javascript(defaults['getsvg'] % card.id, timeout=10)
                    if 'Syntax error in text' in svg:
                        delete_card(i)
            elif name=='Failed':
                with ui.card().tight() as card:
                    with ui.grid(columns=12):
                        with ui.button(on_click=lambda i=i: delete_card(i)).classes('col-start-12'):
                            ui.icon('delete_forever')
                    ui.chat_message(text=content, sent=name=='You')
            else:
                ui.chat_message(text=content, sent=name=='You')

    # Input bar
    with ui.footer().classes('bg-white'), ui.column().classes('w-full max-w-3xl mx-auto my-6'):
        with ui.row().classes('w-full no-wrap items-center'):
            if not user_id:
                ui.button('Login', on_click=lambda: ui.navigate.to('/login'))
            if user_id:
                spinner = ui.spinner('dots', size=32, color='blue')
                spinner.visible = False
                searchbar = ui.input(placeholder=defaults['placeholder']) \
                    .props('rounded outlined input-class=mx-3') \
                    .classes('w-full self-center') \
                    .on('keydown.enter', lambda: search(searchbar, spinner))
                searchbutton = ui.button(" ", on_click=lambda: search(searchbar, spinner)).props(
                    "flat icon=send"
                ).style("transform: rotate(-90deg); font-size: 1.5em; padding: 10px;")

            tokens = retrieve_data(user_id, 'tokens')
            if tokens == None: tokens = defaults['min_tokens']
            if user_id and tokens <= 0:
                searchbar.visible = False
                searchbutton.visible = False
                buy_link = generate_buy_link()
                ui.button(defaults['buy'], on_click=lambda: ui.navigate.to(buy_link))
        ui.markdown(defaults['comment'] % gpt_model) \
            .classes('text-xs self-end mr-8 m-[-1em] text-primary')
    ui.run_javascript('window.scrollTo(0, document.body.scrollHeight)')
    
    with ui.column().classes('w-full mx-auto items-stretch'):
        try:
            await chat_messages()
        except TimeoutError:
            pass

ui.run(title='FlowGenius', host='0.0.0.0', 
        storage_secret=config('STORAGE_SECRET'), 
        reconnect_timeout=30,
        binding_refresh_interval=3,
        favicon='🚀',
        reload=True)
