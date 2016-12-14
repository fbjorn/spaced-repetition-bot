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

from models import Task, TaskStatus, get_current_timestamp
from models import db as database
from utils import encode_callback_data, decode_callback_data, \
    render_template, format_task_content, decode_answer_option

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
    ADD_TASK = 'Do you want to save {}?'
    NOTIFICATION_QUESTION = 'Do you remember the meaning of {}?'
    ADD_CONFIRMATION = 'You will receive reminder about {} soon'
    REGULAR_REPLY = 'As you wish'
    ERROR_MESSAGE = 'Some error with database occured'
    TERM_HAS_LEARNED = 'Awesome! Seems like you\'ve learned {}'
    REMEMBER = 'Good job! Next notification in {} sec'
    FORGOT = 'Notification counter was reset'
    HELP = 'Just write me a term you want to remember'


def handle_text(bot, update):
    user_message = format_task_content(update.message.text)
    encoded_task = encode_callback_data(AnswerOption.ADD_TASK, user_message)

    keyboard = [[
        Button('Yes', callback_data=encoded_task),
        Button('No', callback_data=AnswerOption.CANCEL)
    ]]
    markup = InlineKeyboardMarkup(keyboard)

    text = render_template(
        MessageTemplate.ADD_TASK,
        [user_message],
        bold=True)

    bot.send_message(
        chat_id=update.message.chat_id,
        text=text,
        reply_markup=markup,
        parse_mode=ParseMode.MARKDOWN)


def edit_message(bot, update, text):
    bot.editMessageText(
        text=text,
        chat_id=update.callback_query.message.chat_id,
        message_id=update.callback_query.message.message_id,
        parse_mode=ParseMode.MARKDOWN)


def handle_task_creation_dialog(bot, update):
    callback_data = update.callback_query.data
    answer = decode_answer_option(callback_data)

    # create task
    if answer == AnswerOption.ADD_TASK:
        content = decode_callback_data(callback_data)
        chat_id = update.callback_query.message.chat_id

        Task.create(content=content, chat_id=chat_id)
        reply_text = render_template(
            MessageTemplate.ADD_CONFIRMATION,
            [content],
            bold=True)
        edit_message(bot, update, reply_text)

    # cancel
    elif answer == AnswerOption.CANCEL:
        reply_text = render_template(MessageTemplate.REGULAR_REPLY)
        edit_message(bot, update, reply_text)


def handle_quiz_dialog(bot, update):
    callback_data = update.callback_query.data
    answer = decode_answer_option(callback_data)

    message = update.callback_query.message.text
    chat_id = int(update.callback_query.message.chat_id)
    content = decode_callback_data(callback_data)

    task = Task.find_task(chat_id, content)

    # task not found in DB
    if not task:
        edit_message(bot, update, MessageTemplate.ERROR_MESSAGE)
        return

    # user remember a term
    if answer == AnswerOption.REMEMBER:
        time_interval = task.update_notification_date(remember=True)

        if time_interval:
            reply_text = render_template(
                MessageTemplate.REMEMBER,
                [time_interval])
        else:
            reply_text = render_template(
                MessageTemplate.TERM_HAS_LEARNED,
                [content],
                bold=True)

    # user forgot a term
    elif answer == AnswerOption.FORGOT:
        task.update_notification_date(remember=False)
        reply_text = render_template(MessageTemplate.FORGOT)

    # user want to stop learning term
    elif answer == AnswerOption.REMOVE:
        task.set_status(TaskStatus.DONE)
        reply_text = render_template(MessageTemplate.REGULAR_REPLY)

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
    encoded_yes = encode_callback_data(AnswerOption.REMEMBER, task.content)
    encoded_no = encode_callback_data(AnswerOption.FORGOT, task.content)
    encoded_rem = encode_callback_data(AnswerOption.REMOVE, task.content)

    keyboard = [
        [Button("Yes", callback_data=encoded_yes),
         Button("No", callback_data=encoded_no)],
        [Button('Remove', callback_data=encoded_rem)]
    ]

    markup = InlineKeyboardMarkup(keyboard)
    text = render_template(
        MessageTemplate.NOTIFICATION_QUESTION,
        [task.content],
        bold=True)

    task.set_status(TaskStatus.WAITING_ANSWER)
    bot.send_message(
        chat_id=task.chat_id,
        text=text,
        reply_markup=markup,
        parse_mode=ParseMode.MARKDOWN)


def create_task(content, chat_id):
    with database.transaction():
        Task.create(content=content, chat_id=chat_id)


def error(bot, update, error):
    logging.warning('Update "%s" caused error "%s"' % (update, error))


def task_watcher(bot):
    while True:
        for task in Task.get_active_tasks():
            Thread(target=remind_task_to_user, args=(bot, task)).start()
        time.sleep(1)


def add_handlers(dsp):
    dsp.add_handler(CallbackQueryHandler(callback_handler))
    dsp.add_handler(CommandHandler('help', help))
    dsp.add_handler(MessageHandler(Filters.text, handle_text))
    dsp.add_error_handler(error)


if __name__ == '__main__':
    with open('token') as token_file:
        updater = Updater(token_file.read().strip())

    add_handlers(updater.dispatcher)
    updater.start_polling()

    Thread(target=task_watcher, args=(updater.bot,)).start()
    updater.idle()
