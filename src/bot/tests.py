from unittest import TestCase, main
from unittest.mock import patch, MagicMock
from functools import wraps

from playhouse.test_utils import test_database
from peewee import *

import src.bot.models
import src.bot.bot
from src.bot.models import Task, TaskStatus, User, Activity, TelegramCallback
from src.bot.utils import encode_callback_data, decode_callback_data, \
    render_template, format_task_content, decode_answer_option, \
    timestamp_to_date, load_config, _convert_handwrite_to_seconds
from src.bot.bot import callback_handler, remind_task_to_user, \
    AnswerOption, MessageTemplate


test_db = SqliteDatabase(':memory:')


def wrap_with_test_db(test, tables):

    @wraps(test)
    def decorated(*args, **kwargs):
        with test_database(test_db, (tuple(tables))):
            test(*args, **kwargs)

    return decorated


def with_test_db(*tables):

    def decorated(cls):
        for attr in dir(cls):
            if attr.startswith('test_'):
                test = getattr(cls, attr)
                setattr(cls, attr, wrap_with_test_db(test, tables))
        return cls

    return decorated


class TestUtils(TestCase):

    @patch('src.bot.utils.yaml.load')
    def test_load_config_intervals_multiplier(self, yaml_load):
        yaml_load.return_value = {
            'time_intervals': ['1s', '2m3s']
        }
        config = load_config()
        self.assertEqual(config['time_intervals'], [1, 123])

    def test_convert_handwrite_to_seconds(self):
        minute = 60
        hour = 60 * minute
        day = 24 * hour
        week = 7 * day
        self.assertEqual(_convert_handwrite_to_seconds('7s'), 7)
        self.assertEqual(_convert_handwrite_to_seconds('60s'), minute)
        self.assertEqual(_convert_handwrite_to_seconds('1m'), minute)
        self.assertEqual(_convert_handwrite_to_seconds('1h'), hour)
        self.assertEqual(_convert_handwrite_to_seconds('2d'), 2 * day)
        self.assertEqual(_convert_handwrite_to_seconds('3w'), 3 * week)

        self.assertEqual(_convert_handwrite_to_seconds('5.5d'), 5.5 * day)
        self.assertEqual(_convert_handwrite_to_seconds('0.5h'), 30 * minute)
        self.assertEqual(_convert_handwrite_to_seconds('1.5m'), 1.5 * minute)

        self.assertEqual(_convert_handwrite_to_seconds('1m5s'), 65)
        self.assertEqual(_convert_handwrite_to_seconds('1.5m2.5s'), 92.5)
        self.assertEqual(_convert_handwrite_to_seconds('1w1d'), week + day)


@with_test_db(Task)
class TestTaskCreation(TestCase):

    @patch.object(src.bot.models, 'time_intervals', [5, 10])
    @patch('src.bot.models.get_current_timestamp')
    def test_task_default_creation(self, current_date):
        current_date.return_value = 100
        task = Task.create(chat_id=1, content='stuff')
        self.assertEqual(task.notification_date, current_date() + 5)
        self.assertEqual(task.status, TaskStatus.ACTIVE)

    def test_regular_task_creation(self):
        task = Task.create(content='Hi', chat_id=52)
        self.assertEqual(task.content, 'Hi')
        self.assertEqual(task.chat_id, 52)


@with_test_db(Task, Activity)
class TestUpdateNotificationDate(TestCase):

    def setUp(self):
        patcher = patch('src.bot.models.get_current_timestamp')
        self.addCleanup(patcher.stop)
        self.mock_timestamp = patcher.start()
        self.current_date = 100
        self.mock_timestamp.return_value = self.current_date

    @patch.object(src.bot.models, 'time_intervals', [1, 2, 3])
    def test_user_forgot_a_term(self):
        intervals = src.bot.models.time_intervals

        task = Task.create(chat_id=1, content='stuff')
        delta = intervals[0]
        self.assertEqual(task.notification_date, self.current_date + delta)

        time_gap = task.update_notification_date(remember=False)
        new_date = task.notification_date

        self.assertEqual(new_date, self.current_date + intervals[0])
        self.assertEqual(time_gap, intervals[0])
        self.assertEqual(task.status, TaskStatus.ACTIVE)

        time_gap = task.update_notification_date(remember=False)
        self.assertEqual(time_gap, intervals[0])
        self.assertEqual(task.status, TaskStatus.ACTIVE)

    @patch.object(src.bot.models, 'time_intervals', [1, 2, 3])
    def test_user_remember_a_term(self):
        intervals = src.bot.models.time_intervals

        task = Task.create(chat_id=1, content='stuff')

        task.update_notification_date(remember=True)
        new_date = task.notification_date
        self.assertEqual(new_date, self.current_date + intervals[1])
        self.assertEqual(task.status, TaskStatus.ACTIVE)

        task.update_notification_date(remember=True)
        new_date = task.notification_date
        self.assertEqual(new_date, self.current_date + intervals[2])
        self.assertEqual(task.status, TaskStatus.ACTIVE)

    @patch.object(src.bot.models, 'time_intervals', [2, 3])
    def test_maximum_intervals_reached(self):
        task = Task.create(chat_id=1, content='stuff')
        task.update_notification_date(remember=True)
        time_gap = task.update_notification_date(remember=True)
        self.assertFalse(time_gap)
        self.assertEqual(task.status, TaskStatus.DONE)


@with_test_db(Task, Activity)
class TestModelsCommonOperations(TestCase):

    def test_change_status(self):
        task = Task.create(chat_id=1, content='stuff')
        task.set_status(TaskStatus.DONE)
        self.assertEqual(task.status, TaskStatus.DONE)
        task.set_status(TaskStatus.ACTIVE)
        self.assertEqual(task.status, TaskStatus.ACTIVE)

    @patch('src.bot.models.get_current_timestamp')
    def test_mark_done(self, current_date):
        task = Task.create(chat_id=1, content='stuff')
        task.mark_done()
        self.assertEqual(task.status, TaskStatus.DONE)
        self.assertEqual(task.finish_date, current_date())

    def test_forgot_counter(self):
        task = Task.create(chat_id=1, content='stuff')
        self.assertEqual(task.forgot_counter, 0)
        task.update_notification_date(remember=True)
        self.assertEqual(task.forgot_counter, 0)
        task.update_notification_date(remember=False)
        task.update_notification_date(remember=False)
        self.assertEqual(task.forgot_counter, 2)
        task.update_notification_date(remember=True)
        self.assertEqual(task.forgot_counter, 2)

    def test_inc_forgot_counter(self):
        task = Task.create(chat_id=1, content='stuff')
        task.increase_forgot_counter()
        self.assertEqual(task.forgot_counter, 1)
        task.increase_forgot_counter(20)
        self.assertEqual(task.forgot_counter, 21)

    def test_find_task(self):
        Task.create(content='abc', chat_id=1)
        Task.create(content='abc', chat_id=2)
        Task.create(content='def', chat_id=1)
        Task.create(content='def', chat_id=2)

        task = Task.find_task(2, 'qqq')
        self.assertEqual(task, None)
        task = Task.find_task(2, 'def')
        self.assertEqual(task.chat_id, 2)
        self.assertEqual(task.content, 'def')

    @patch('src.bot.models.get_current_timestamp')
    def test_get_active_tasks(self, current_date):
        current_date.return_value = 0

        self.assertEqual(Task.get_active_tasks(), [])

        Task.create(status=TaskStatus.ACTIVE, chat_id=1, content='1')
        Task.create(status=TaskStatus.ACTIVE, chat_id=1, content='2')
        Task.create(status=TaskStatus.DONE, chat_id=1, content='3')
        Task.create(
            status=TaskStatus.WAITING_ANSWER, chat_id=1, content='4')
        current_date.return_value = 999999
        tasks = Task.get_active_tasks()

        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[0].status, TaskStatus.ACTIVE)
        self.assertEqual(tasks[1].status, TaskStatus.ACTIVE)

    def test_get_users_tasks(self):
        Task.create(chat_id=1, content='first')
        Task.create(chat_id=7, content='second')
        Task.create(chat_id=7, content='third')

        self.assertEqual(len(Task.get_users_tasks(1)), 1)
        self.assertEqual(Task.get_users_tasks(1)[0].content, 'first')
        self.assertEqual(len(Task.get_users_tasks(7)), 2)


class TestBotCommon(TestCase):

    def test_encode_callback(self):
        data = encode_callback_data(1, 'text')
        self.assertEqual(data, '1@text')

        data = encode_callback_data(2, '@ text')
        self.assertEqual(data, '2@@ text')

        data = encode_callback_data(13, '1')
        self.assertEqual(data, '13@1')

    def test_decode_callback(self):
        data = encode_callback_data(1, 'message')
        self.assertEqual(decode_callback_data(data), 'message')

        data = encode_callback_data(12, '@text@')
        self.assertEqual(decode_callback_data(data), '@text@')

        data = encode_callback_data(0, ' one two ')
        self.assertEqual(decode_callback_data(data), ' one two ')

    def test_decode_answer_option(self):
        data = encode_callback_data(1, 'message')
        self.assertEqual(decode_answer_option(data), '1')

        data = encode_callback_data(12, '@12@')
        self.assertEqual(decode_answer_option(data), '12')

    def test_render_template(self):
        text = render_template('The {} who knocks', 'one')
        self.assertEqual(text, 'The one who knocks')

        text = render_template('{} {}', 2, 'args')
        self.assertEqual(text, '2 args')

        text = render_template('{} text', 'bold', bold=True)
        self.assertEqual(text, '<b>bold</b> text')

        self.assertEqual(render_template('Pure text'), 'Pure text')

        with self.assertRaises(IndexError):
            render_template('{} {} {}', 1)

        with self.assertRaises(IndexError):
            render_template('{}', 1, 2, 3)

    def test_format_content(self):
        content = ' Strip '
        self.assertEqual(format_task_content(content), 'Strip')

        content = 'upper'
        self.assertEqual(format_task_content(content), 'Upper')

        self.assertEqual(format_task_content(' a '), format_task_content('A'))

    def test_from_timestamp(self):
        pretty_date = timestamp_to_date(0)
        self.assertIn('01/01/1970', pretty_date)


class BotTestCase(TestCase):

    def setUp(self):
        self.chat_id = 777
        message = MagicMock(chat_id=self.chat_id, message_id=111)
        callback_query = MagicMock(message=message, data='')
        self.update = MagicMock(callback_query=callback_query)
        self.bot = MagicMock()

        patcher = patch('src.bot.bot.render_template')
        self.addCleanup(patcher.stop)
        self.mock_render = patcher.start()

    def answer(self, answer_option, text):
        ''' Emulate user click on a callback button'''

        if answer_option in (AnswerOption.ADD_TASK, AnswerOption.CANCEL):
            callback = TelegramCallback.create(data=text)
            data = encode_callback_data(answer_option, callback.id)
        else:
            task = Task.find_task(self.chat_id, text)
            data = encode_callback_data(answer_option, task.id)

        self.update.callback_query.data = data
        callback_handler(self.bot, self.update)

    def assertRendered(self, message_template):
        self.assertIn(message_template, self.mock_render.call_args[0])


@with_test_db(Task, Activity, TelegramCallback)
class TestBotCallbacks(BotTestCase):

    def test_add_task(self):
        self.answer(AnswerOption.ADD_TASK, 'python')

        self.assertEqual(Task.select(), 1)
        task = Task.find_task(777, 'python')
        self.assertIsNotNone(task)
        self.assertEqual(task.status, TaskStatus.ACTIVE)
        self.assertRendered(MessageTemplate.ADD_CONFIRMATION)

    def test_remove_task(self):
        Task.create(chat_id=777, content='python')
        self.answer(AnswerOption.REMOVE, 'python')

        task = Task.get()
        self.assertEqual(task.status, TaskStatus.DONE)
        self.assertGreater(task.finish_date, 0)
        self.assertRendered(MessageTemplate.REMOVAL_CONFIRM)

    @patch.object(src.bot.models.Task, 'update_notification_date')
    def test_user_remember_task(self, update_date):
        self.answer(AnswerOption.ADD_TASK, 'stuff')

        self.answer(AnswerOption.REMEMBER, 'stuff')

        update_date.assert_called_with(remember=True)
        self.assertRendered(MessageTemplate.REMEMBER)

    @patch.object(src.bot.models.Task, 'update_notification_date')
    def test_user_forgot_task(self, update_date):
        self.answer(AnswerOption.ADD_TASK, 'thing')

        self.answer(AnswerOption.FORGOT, 'thing')

        update_date.assert_called_with(remember=False)
        self.assertRendered(MessageTemplate.FORGOT)

    def test_cancel_task_creation(self):
        self.answer(AnswerOption.CANCEL, 'text')

        self.assertEqual(len(Task.select()), 0)
        self.assertRendered(MessageTemplate.REGULAR_REPLY)

    def test_task_reminder(self):
        task = Task.create(chat_id=777, content='Content')
        remind_task_to_user(self.bot, task)

        self.assertEqual(task.status, TaskStatus.WAITING_ANSWER)
        self.assertRendered(MessageTemplate.NOTIFICATION_QUESTION)

    @patch.object(src.bot.models, 'time_intervals', [5])
    def test_user_has_learned_term(self):
        self.answer(AnswerOption.ADD_TASK, 'Quick')
        self.answer(AnswerOption.REMEMBER, 'Quick')

        self.assertRendered(MessageTemplate.TERM_HAS_LEARNED)
        self.assertEqual(Task.get().status, TaskStatus.DONE)
        self.assertGreater(Task.get().finish_date, 0)

    def test_add_existing_active_task(self):
        self.answer(AnswerOption.ADD_TASK, 'JS')
        self.answer(AnswerOption.ADD_TASK, 'JS')

        self.assertRendered(MessageTemplate.DUPLICATE_ACTIVE_TASK)
        self.assertEqual(len(Task.select()), 1)
        self.assertEqual(Task.get().forgot_counter, 1)

    def test_add_finished_task(self):
        self.answer(AnswerOption.ADD_TASK, 'JS')
        self.answer(AnswerOption.REMOVE, 'JS')
        self.answer(AnswerOption.ADD_TASK, 'JS')

        self.assertRendered(MessageTemplate.DUPLICATE_DONE_TASK)
        self.assertEqual(len(Task.select()), 1)
        self.assertEqual(Task.get().forgot_counter, 1)


@with_test_db(User)
class TestUser(TestCase):

    @patch.object(src.bot.models, 'API_KEY_SIZE', 100)
    def test_generate_api_key(self):
        user = User.create(chat_id=777)
        api_key = user.generate_api_key()
        chat_id, key = api_key.split(':')
        self.assertEqual(user.chat_id, int(chat_id))
        self.assertEqual(len(key), 100)

        another_key = user.generate_api_key()
        self.assertNotEqual(api_key, another_key)

    def test_find(self):
        user = User.create(chat_id=777)
        api_key = user.generate_api_key()
        target = User.find(777)
        self.assertEqual(target.api_key, api_key)

    def test_find_creates_new_instance(self):
        user = User.find(chat_id=999)
        self.assertEqual(user.chat_id, 999)
        self.assertGreater(len(user.api_key), 1)
        self.assertEqual(len(User.select()), 1)


@with_test_db(Activity)
class TestActivity(TestCase):

    def test_get_or_create(self):
        record = Activity.get(chat_id=1)
        record.remember_count = 1337
        record.save()

        same_rec = Activity.get(chat_id=1)
        self.assertEqual(same_rec.remember_count, 1337)
        self.assertEqual(len(Activity.select()), 1)

        Activity.get(chat_id=2)
        self.assertEqual(len(Activity.select()), 2)

    def test_increment(self):
        Activity.increment(42, Activity.ADD_BOT)
        self.assertEqual(Activity.get(chat_id=42).bot_add, 1)

        Activity.increment(42, Activity.ADD_EXT)
        self.assertEqual(Activity.get(chat_id=42).ext_add, 1)

        Activity.increment(42, Activity.REMEMBER)
        self.assertEqual(Activity.get(chat_id=42).remember_count, 1)

        Activity.increment(42, Activity.FORGOT)
        self.assertEqual(Activity.get(chat_id=42).forgot_count, 1)

    def test_get_user_data(self):
        self.assertEqual(Activity.get_user_data(13), [])
        Activity.increment(13, Activity.REMEMBER)
        self.assertEqual(len(Activity.get_user_data(13)), 1)

    def test_get_public_list(self):
        self.assertEqual(Activity.get_public_list(555), [])
        Activity.increment(555, Activity.ADD_BOT)
        self.assertEqual(len(Activity.get_public_list(555)), 1)


@with_test_db(Task, Activity, TelegramCallback)
class TestActivityWithBot(BotTestCase):

    def test_increment(self):
        self.answer(AnswerOption.ADD_TASK, 'JS')
        self.answer(AnswerOption.REMEMBER, 'JS')
        self.answer(AnswerOption.FORGOT, 'JS')

        self.assertEqual(Activity.get(self.chat_id).bot_add, 1)
        self.assertEqual(Activity.get(self.chat_id).remember_count, 1)
        self.assertEqual(Activity.get(self.chat_id).forgot_count, 1)

    def test_add_duplicated_task(self):
        self.answer(AnswerOption.ADD_TASK, 'JS')
        self.answer(AnswerOption.ADD_TASK, 'JS')
        self.assertEqual(Activity.get(self.chat_id).bot_add, 1)
        self.assertEqual(Activity.get(self.chat_id).forgot_count, 1)


@with_test_db(TelegramCallback)
class TestTelegramCallback(BotTestCase):

    def test_auto_delete(self):
        callback = TelegramCallback.create(data='Hey')
        bot_callback = encode_callback_data(1, callback.id)
        data = TelegramCallback.pop_data(bot_callback)

        self.assertEqual(len(TelegramCallback.select()), 0)
        self.assertEqual(data, 'Hey')

    def test_delete_if_user_press_cancel(self):
        self.answer(AnswerOption.CANCEL, 'Cancel')
        self.assertEqual(len(TelegramCallback.select()), 0)


if __name__ == '__main__':
    main()
