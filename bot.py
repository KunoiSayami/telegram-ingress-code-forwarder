#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# bot.py
# Copyright (C) 2020-2021 KunoiSayami
#
# This module is part of telegram-ingress-code-forwarder and is released under
# the AGPL v3 License: https://www.gnu.org/licenses/agpl-3.0.txt
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
import ast
import asyncio
import logging
import re
import sys
from configparser import ConfigParser
from typing import List

import aioredis
import pyrogram
from pyrogram import Client, filters
from pyrogram.handlers import CallbackQueryHandler, MessageHandler
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

try:
    from libsqlite import PasscodeTracker
except ImportError as e:
    try:
        from forwarder.libsqlite import PasscodeTracker
    except ImportError:
        raise e

logger = logging.getLogger('code_poster')

PASSCODE_EXP = re.compile(r'^\w{5,35}$')


class Tracker:
    def __init__(self, api_id: int, api_hash: str, bot_token: str, conn: PasscodeTracker, channel_id: int,
                 password: str, owners: str, redis: aioredis.Redis):
        self.app = Client('passcode', api_id=api_id, api_hash=api_hash, bot_token=bot_token)
        self.conn = conn
        self.channel_id = channel_id
        self.password = password
        self.owners: List[int] = ast.literal_eval(owners)
        self.redis = redis
        self.init_message_handler()

    def init_message_handler(self) -> None:
        self.app.add_handler(MessageHandler(self.handle_auth, filters.command('auth') & filters.private))
        self.app.add_handler(MessageHandler(self.pre_check, filters.text & filters.private))
        self.app.add_handler(MessageHandler(self.handle_passcode, filters.text & filters.private))
        self.app.add_handler(CallbackQueryHandler(self.handle_callback_query))
        self.app.add_handler(MessageHandler(self.pre_check_owner, filters.text & filters.private))
        self.app.add_handler(MessageHandler(self.query_history, filters.command('h') & filters.private))
        self.app.add_handler(MessageHandler(self.delete_user_manual, filters.command('del') & filters.private))

    async def start(self) -> None:
        await asyncio.gather(self.app.start(), self._load_users())

    @staticmethod
    async def idle() -> None:
        await pyrogram.idle()

    async def stop(self) -> None:
        await asyncio.gather(self.app.stop(),
                             self.redis.close())

    @classmethod
    async def load_from_config(cls, config: ConfigParser, *, debug: bool = False, database_file: str = 'codes.db'):
        return await cls.new(config.getint('telegram', 'api_id'), config.get('telegram', 'api_hash'),
                             config.get('telegram', 'bot_token'), database_file, config.getint('telegram', 'channel'),
                             config.get('telegram', 'password'), config.get('telegram', 'owners', fallback='[]'),
                             debug_mode=debug)

    @classmethod
    async def new(cls, api_id: int, api_hash: str, bot_token: str, file_name: str, channel_id: int,
                  password: str, owners: str, *, debug_mode: bool = False) -> 'Tracker':
        self = cls(api_id, api_hash, bot_token, await PasscodeTracker.new(file_name, renew=debug_mode),
                   channel_id, password, owners, await aioredis.from_url('redis://localhost'))
        return self

    async def handle_passcode(self, client: Client, msg: Message) -> None:
        if '\n' in msg.text:
            return await self.handle_multiline_passcode(client, msg)
        if len(msg.text) > 30:
            await msg.reply("Passcode length exceed")
            return
        if PASSCODE_EXP.match(msg.text) is None:
            await msg.reply("Passcode format error")
            return
        result = await self.conn.query(msg.text)
        if result is None:
            _msg = await client.send_message(self.channel_id, f'<code>{msg.text}</code>', 'html')
            await asyncio.gather(self.conn.insert(msg.text, _msg.message_id),
                                 self.conn.insert_history(msg.text, msg.chat.id),
                                 self.hook_send_passcode(msg.text))
            await msg.reply('Send successful')
        else:
            await msg.reply(f"Passcode exist, {'mark passcode' if not result.FR else 'undo mark'} as FR?",
                            reply_markup=InlineKeyboardMarkup([[
                                InlineKeyboardButton(
                                    "Process", f"{'u' if result.FR else 'm'} {msg.text} {result.message_id}")]]))

    async def hook_send_passcode(self, passcode: str) -> None:
        pass

    async def hook_mark_full_redeemed_passcode(self, passcode: str, is_fr: bool = False) -> None:
        pass

    @staticmethod
    def parse_codes(passcodes: List[str], header: str) -> str:
        nl = '\n'
        return f'{header} codes:\n<code>{f"</code>{nl}<code>".join(passcodes)}</code>\n' if passcodes else ''

    async def handle_multiline_passcode(self, client: Client, msg: Message) -> None:
        count = 0
        error_codes = []
        duplicate_codes = []
        status_message = None
        for passcode in msg.text.splitlines(False):
            if passcode == '' or passcode.startswith('#'):
                continue
            if len(passcode) > 35 or PASSCODE_EXP.match(passcode) is None:
                error_codes.append(passcode)
                continue
            if await self.conn.query(passcode) is None:
                if status_message is None:
                    status_message = await msg.reply('Sending passcode (interval: 2s)')
                _msg = await client.send_message(self.channel_id, f'<code>{passcode}</code>', 'html')
                count += 1
                await asyncio.gather(self.conn.insert(passcode, _msg.message_id),
                                     self.conn.insert_history(passcode, msg.chat.id),
                                     asyncio.sleep(2),
                                     self.hook_send_passcode(passcode))
            else:
                duplicate_codes.append(passcode)
        error_msg = self.parse_codes(error_codes, 'Error')
        duplicate_msg = self.parse_codes(duplicate_codes, 'Duplicate')
        if status_message is None:
            edit_func = msg.reply
        else:
            edit_func = status_message.edit
        await edit_func(f'{error_msg}\n{duplicate_msg}\nSuccess send: {count} passcode(s)')

    async def handle_callback_query(self, client: Client, msg: CallbackQuery) -> None:
        args = msg.data.split()
        if len(args) != 3:
            if args[0] == 'ignore':
                await asyncio.gather(msg.edit_message_reply_markup(), msg.answer())
            return

        # Account process
        if args[0] == 'account':
            _arg, sub_arg, user_id = args
            user_id = int(user_id)
            if sub_arg == 'grant':
                answer_msg = None
                if await self.query_authorized_user(user_id):
                    answer_msg = 'Already granted'
                else:
                    await self.insert_authorized_user(user_id)
                await asyncio.gather(msg.message.edit_reply_markup(
                    InlineKeyboardMarkup([[
                        InlineKeyboardButton('Revoke', f'account revoke {user_id}')
                    ]])
                ), msg.answer(answer_msg), client.send_message(user_id, 'Access granted'))
            elif sub_arg == 'deny':
                if await self.query_authorized_user(user_id):
                    await asyncio.gather(msg.message.edit_reply_markup(), msg.answer('Out of dated'))
                    return
                await asyncio.gather(msg.message.edit_reply_markup(),
                                     client.send_message(user_id, 'Access denied'), msg.answer())
            elif sub_arg == 'revoke':
                await self.delete_authorized_user(user_id)
                await asyncio.gather(msg.message.edit_reply_markup(),
                                     client.send_message(user_id, "Access revoked"), msg.answer())
            return
        # Account end

        _msg_text = f'<del>{args[1]}</del>' if args[0] == 'm' else f'<code>{args[1]}</code>'
        await asyncio.gather(
            client.edit_message_text(self.channel_id, int(args[2]), _msg_text, 'html'),
            self.conn.update(args[1], args[0] == 'm'),
            self.hook_mark_full_redeemed_passcode(args[1], args[0] == 'm'),
            msg.edit_message_reply_markup(),
            msg.answer(),
        )

    async def handle_auth(self, client: Client, msg: Message) -> None:
        if await self.flood_check(msg.chat.id, 1200):
            return
        if await self.query_authorized_user(msg.chat.id):
            await msg.reply('Already authorized')
            return
        if len(msg.command) == 1:
            logger.debug('User %d request to grant talk power', msg.chat.id)
            await asyncio.gather(*[client.send_message(
                owner,
                f"User [{msg.chat.id}](tg://user?id={msg.chat.id}) request to grant talk power",
                'markdown',
                reply_markup=InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton('Agree', f'account grant {msg.chat.id}'),
                         InlineKeyboardButton('Deny', f'account deny {msg.chat.id}')],
                        [InlineKeyboardButton('Ignore', 'ignore')]
                    ])) for owner in self.owners])
        elif len(msg.command) == 2 and msg.command[1] == self.password:
            await self.insert_authorized_user(msg.chat.id)
            if len(self.owners):
                await msg.reply('Authorized')
            else:
                self.owners.append(msg.chat.id)
                await msg.reply('Authorized as owner')

    async def pre_check(self, _client: Client, msg: Message) -> None:
        if await self.query_authorized_user(msg.chat.id):
            msg.continue_propagation()

    async def query_authorized_user(self, user_id: int) -> bool:
        return await self.redis.sismember('tracker_user', str(user_id))

    async def insert_authorized_user(self, user_id: int) -> None:
        logger.info('Insert user %d to database', user_id)
        await asyncio.gather(self.conn.insert_user(user_id),
                             self.redis.sadd("tracker_user", str(user_id)))

    async def delete_authorized_user(self, user_id: int) -> None:
        logger.info('Delete user %d from database', user_id)
        await asyncio.gather(self.conn.delete_user(user_id),
                             self.redis.srem("tracker_user", str(user_id)))

    async def flood_check(self, user_id: int, timeout: int = 120) -> bool:
        if await self.redis.get(f'flood_{user_id}') is None:
            await self.redis.set(f'flood_{user_id}', '1', ex=timeout)
            return False
        return True

    async def _load_users(self) -> None:
        await self.redis.delete('tracker_user')
        async for x in self.conn.query_all_user():
            await self.redis.sadd('tracker_user', str(x))
        if not self.owners:
            logger.warning('Not owners ')
        for x in self.owners:
            await self.redis.sadd('tracker_user', str(x))
        logger.info('Load users successful')

    async def pre_check_owner(self, _client: Client, msg: Message) -> None:
        if msg.chat.id in self.owners:
            msg.continue_propagation()

    async def query_history(self, _client: Client, msg: Message) -> None:
        if len(msg.command) > 2:
            await msg.reply('Query format error.')
            return
        _, code = msg.command
        query_obj = await self.conn.query_history(code)
        if query_obj is not None:
            await msg.reply(f'Find match => {query_obj[0]}')
        else:
            await msg.reply('404 Not found')

    async def delete_user_manual(self, client: Client, msg: Message) -> None:
        if len(msg.command) != 2:
            user_id = int(msg.command[1])
            if await self.query_authorized_user(user_id):
                await asyncio.gather(client.send_message(user_id, "Access revoked"),
                                     self.delete_authorized_user(user_id),
                                     msg.reply('Success'))
            else:
                await msg.reply('User not in authorized list')


async def main(debug: bool = False):
    config = ConfigParser()
    config.read('config.ini')
    bot = await Tracker.load_from_config(config, debug=debug)
    await bot.start()
    await bot.idle()
    await bot.stop()


if __name__ == '__main__':
    try:
        import coloredlogs
        coloredlogs.install(logging.DEBUG,
                            fmt='%(asctime)s,%(msecs)03d - %(levelname)s - %(funcName)s - %(lineno)d - %(message)s')
    except ModuleNotFoundError:
        logging.basicConfig(level=logging.DEBUG,
                            format='%(asctime)s - %(levelname)s - %(funcName)s - %(lineno)d - %(message)s')
    logging.getLogger('pyrogram').setLevel(logging.WARNING)
    logging.getLogger('aiosqlite').setLevel(logging.WARNING)
    asyncio.get_event_loop().run_until_complete(main('--debug' in sys.argv))
