# -*- coding: utf-8 -*-

import logging
from collections import namedtuple
from threading import Thread
import time
import re

from peewee import SqliteDatabase
from telegram import InlineKeyboardButton as Button
from telegram import InlineKeyboardMarkup, ParseMode
from telegram.ext import Updater, CommandHandler, CallbackQueryHandler, \
    MessageHandler, Filters

from src.bot.models import Task, TaskStatus, User, Activity, \
    get_current_timestamp, TelegramCallback
from src.bot.utils import encode_callback_data, decode_callback_data, \
    render_template, format_task_content, decode_answer_option, \
    timestamp_to_date, load_config

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')


class AnswerOption(object):
    REMEMBER = '1'
    FORGOT = '2'
    REMOVE = '3'
    ADD_TASK = '4'
    CANCEL = '5'


class MessageTemplate(object):
    ADD_TASK = 'Do you want to start learning {}?'
    NOTIFICATION_QUESTION = 'Do you remember the meaning of {}?'
    ADD_CONFIRMATION = 'You will receive reminder about {} soon'
    REGULAR_REPLY = 'As you wish'
    REMOVAL_CONFIRM = '{} was removed from reminder list'
    ERROR_MESSAGE = 'Some error with database occured'
    TERM_HAS_LEARNED = 'Awesome! Seems like you\'ve learned {} 👍'
    REMEMBER = '✅ Good job! You are still remember {} ✅'
    FORGOT = '❌ Notification counter for {} was reset ❌'
    DUPLICATE_ACTIVE_TASK = (
        'You\'re already learning {}. '
        'I\'m resetting notification counter')
    DUPLICATE_DONE_TASK = (
        'First time you start learning {} at {}. Then you finished at {}. '
        'I\'m resetting notification counter')
    HELP = 'Just write me a term you want to remember'


def handle_text(bot, update):
    user_message = format_task_content(update.message.text)
    callback = TelegramCallback.create(data=user_message)
    encoded_task = encode_callback_data(AnswerOption.ADD_TASK, callback.id)

    keyboard = [[
        Button('Yes', callback_data=encoded_task),
        Button('No', callback_data=AnswerOption.CANCEL)
    ]]
    markup = InlineKeyboardMarkup(keyboard)

    text = render_template(MessageTemplate.ADD_TASK, user_message, bold=True)

    bot.send_message(
        chat_id=update.message.chat_id,
        text=text,
        reply_markup=markup,
        parse_mode=ParseMode.HTML)


def edit_message(bot, update, text):
    bot.editMessageText(
        text=text,
        chat_id=update.callback_query.message.chat_id,
        message_id=update.callback_query.message.message_id,
        parse_mode=ParseMode.HTML)


def handle_task_creation_dialog(bot, update):
    callback_data = update.callback_query.data
    answer = decode_answer_option(callback_data)

    # create task
    if answer == AnswerOption.ADD_TASK:
        content = TelegramCallback.pop_data(callback_data)
        chat_id = update.callback_query.message.chat_id

        task = Task.create(
            content=content,
            chat_id=chat_id,
            origin=Activity.ADD_BOT)

        if task.forgot_counter == 0:
            reply_text = render_template(
                MessageTemplate.ADD_CONFIRMATION, content, bold=True)

        elif task.finish_date:
            reply_text = render_template(
                MessageTemplate.DUPLICATE_DONE_TASK,
                content,
                timestamp_to_date(task.start_date),
                timestamp_to_date(task.finish_date))

        elif task.forgot_counter:
            reply_text = render_template(
                MessageTemplate.DUPLICATE_ACTIVE_TASK, content)

    # cancel
    elif answer == AnswerOption.CANCEL:
        reply_text = render_template(MessageTemplate.REGULAR_REPLY)
        TelegramCallback.pop_data(decode_callback_data(callback_data))

    edit_message(bot, update, reply_text)


def handle_quiz_dialog(bot, update):
    callback_data = update.callback_query.data
    answer = decode_answer_option(callback_data)

    message = update.callback_query.message.text
    chat_id = int(update.callback_query.message.chat_id)
    task = Task.from_callback(callback_data)

    # task not found in DB
    if not task:
        edit_message(bot, update, MessageTemplate.ERROR_MESSAGE)
        return

    # user remember a term
    if answer == AnswerOption.REMEMBER:
        time_interval = task.update_notification_date(remember=True)

        if time_interval:
            reply_text = render_template(
                MessageTemplate.REMEMBER, task.content)
        else:
            reply_text = render_template(
                MessageTemplate.TERM_HAS_LEARNED, task.content, bold=True)

    # user forgot a term
    elif answer == AnswerOption.FORGOT:
        task.update_notification_date(remember=False)
        reply_text = render_template(MessageTemplate.FORGOT, task.content)

    # user want to stop learning term
    elif answer == AnswerOption.REMOVE:
        task.mark_done()
        reply_text = render_template(
            MessageTemplate.REMOVAL_CONFIRM, task.content)

    edit_message(bot, update, reply_text)


def callback_handler(bot, update):
    answer = decode_answer_option(update.callback_query.data)

    task_creation_answers = (
        AnswerOption.ADD_TASK,
        AnswerOption.CANCEL,)

    quiz_answers = (
        AnswerOption.REMEMBER,
        AnswerOption.FORGOT,
        AnswerOption.REMOVE,)

    if answer in task_creation_answers:
        handle_task_creation_dialog(bot, update)

    elif answer in quiz_answers:
        handle_quiz_dialog(bot, update)


def help(bot, update):
    update.message.reply_text(render_template(MessageTemplate.HELP))


def remind_task_to_user(bot, task):
    encoded_yes = encode_callback_data(AnswerOption.REMEMBER, task.id)
    encoded_no = encode_callback_data(AnswerOption.FORGOT, task.id)
    encoded_rem = encode_callback_data(AnswerOption.REMOVE, task.id)

    keyboard = [
        [Button("Yes", callback_data=encoded_yes),
         Button("No", callback_data=encoded_no)],
        [Button('Remove', callback_data=encoded_rem)]
    ]

    markup = InlineKeyboardMarkup(keyboard)
    text = render_template(
        MessageTemplate.NOTIFICATION_QUESTION, task.content, bold=True)

    task.set_status(TaskStatus.WAITING_ANSWER)
    bot.send_message(
        chat_id=task.chat_id,
        text=text,
        reply_markup=markup,
        parse_mode=ParseMode.HTML)


def error(bot, update, error):
    logging.warning('Update "%s" caused error "%s"' % (update, error))


def task_watcher(bot):
    while True:
        for task in Task.get_active_tasks():
            Thread(target=remind_task_to_user, args=(bot, task)).start()
        time.sleep(10)


def get_api_key(bot, update):
    api_key = User.find(update.message.chat_id).api_key
    update.message.reply_text(api_key)


def get_stats_creator(base_url):

    def get_stats_url(bot, update):
        url = base_url + str(update.message.chat_id)
        update.message.reply_text(url)

    return get_stats_url


def add_handlers(dsp, config):
    dsp.add_handler(CallbackQueryHandler(callback_handler))
    dsp.add_handler(CommandHandler('help', help))
    dsp.add_handler(CommandHandler('apikey', get_api_key))
    dsp.add_handler(MessageHandler(Filters.text, handle_text))
    dsp.add_error_handler(error)

    stats_url = config.get('stats_url')
    if stats_url:
        dsp.add_handler(CommandHandler('stats', get_stats_creator(stats_url)))




if __name__ == '__main__':
    config = load_config()

    updater = Updater(config['bot_token'])

    add_handlers(updater.dispatcher, config)
    updater.start_polling()

    Thread(target=task_watcher, args=(updater.bot,)).start()
    updater.idle()
