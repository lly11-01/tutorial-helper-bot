"""
VERSION: 0.3
Copyright (C) 2023 Loy Liang Yi
You may use, distribute and modify this code under the terms of the GNU General Public License v3.0.
"""
import io
import logging
from typing import Dict, Optional

import telegram
from telegram import __version__ as TG_VER
from telegram.ext.filters import MessageFilter
from pprint import pprint

try:
    from telegram import __version_info__
except ImportError:
    __version_info__ = (0, 0, 0, 0, 0)  # type: ignore[assignment]

if __version_info__ < (20, 0, 0, "alpha", 1):
    raise RuntimeError(
        f"This example is not compatible with your current PTB version {TG_VER}. To view the "
        f"{TG_VER} version of this example, "
        f"visit https://docs.python-telegram-bot.org/en/v{TG_VER}/examples.html"
    )
from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    PicklePersistence,
    filters,
)
from telegram.constants import ChatMemberStatus, ChatType

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
# set higher logging level for httpx to avoid all GET and POST requests being logged
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

TOKEN = "INSERT_TOKEN_HERE"
SAVE_FILE = "user_data.json"


class Session:
    def __init__(self, number, *qns):
        self.tut_num = number
        self.questions = {q: None for q in qns}

    @property
    def name(self):
        return "Tut "+self.tut_num

    @property
    def keyboard(self):
        """
        Generates a 3-wide reply get_keyboard based on the questions dict.
        The options of the keyboard are the keys of the dictionary whose values are None.
        Includes a remove option as well.
        """
        # edit width here
        size = 3

        avail_qns = [q for q in self.questions if self.questions[q] is None]
        avail_qns.append("Remove")
        avail_qns = [avail_qns[i:i + size] for i in range(0, len(avail_qns), size)]
        return ReplyKeyboardMarkup(avail_qns, one_time_keyboard=True)


class QuestionFilter(MessageFilter):
    """
    Only listens to messages whose text is one of the questions in the active session.
    """

    def __init__(self, filter, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.msg_filter = filter

    def filter(self, message):
        if not self.msg_filter:
            return False
        return message.text in self.msg_filter or message.text == "Remove"


sentinel = None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Initializes the bot.
    """
    global sentinel
    if sentinel is not None:
        reply_msg = await update.message.reply_text("Bot is already initialized")

        await update.message.delete()
        await reply_msg.delete()
        return

    # Wipe chat data and user data
    context.chat_data.clear()
    context.user_data.clear()
    
    context.chat_data['active'] = None
    context.chat_data['volunteer_freqs'] = {}
    context.chat_data['logs'] = {}

    reply_msg = await update.message.reply_text("Beep boop, bot is ready!")
    sentinel = object()
    await update.message.delete()
    # await reply_msg.delete()


async def check_if_admin(update) -> bool:
    """
    Helper function to check if the user running the command is an admin,
    otherwise tell them they are not permitted to use it.
    """
    chat = update.message.chat
    # print(chat.type)
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        logging.error("ERROR: Not implemented for non-group chats")
        return False
    user = await chat.get_member(user_id=update.message.from_user.id)
    # print(user.status)
    if user.status in (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR):
        return True
    message = "You are not permitted to access this command."
    reply_msg = await update.message.reply_text(message)
    await reply_msg.delete()
    return False


async def display(session, update, context):
    """
    Helper function to display and pin the question list for the active tutorial session.
    """
    # Unpin and delete previous display, if there is one
    prev_display = context.chat_data.get('current_display', None)
    if prev_display:
        await prev_display.unpin()
        await prev_display.delete()

    text = io.StringIO()
    text.write(f"Questions for tutorial {session.tut_num}\n")
    for question, doer in session.questions.items():
        text.write(f"Q{question} - {doer.username if doer else ''}\n")

    # Send and pin the question list
    sent_message = await update.message.reply_text(text.getvalue(),
                                                   reply_markup=session.keyboard)
    context.chat_data['current_display'] = sent_message
    await sent_message.pin(disable_notification=True)


async def new_tut(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Creates a new tutorial session and sets it to be active.
    Admin-only command.
    """
    if await check_if_admin(update):
        # End active session if there is one
        prev_session = context.chat_data['active']
        if prev_session is not None:
            await end_tut(update, context)

        # Create new session
        # First argument is the tutorial session number,
        # remaining arguments are the question numbers that can be attempted
        tut_num, *qns = context.args
        new_session = Session(tut_num, *qns)
        context.chat_data['active'] = new_session

        # Update the options listening filter
        global current_filter
        current_filter.msg_filter = new_session.questions

        await display(new_session, update, context)

    await update.message.delete()


async def end_tut(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Ends the currently running tutorial session.
    Admin-only command.
    """
    if await check_if_admin(update):
        # Set previous session to not be active,
        # also finalizes and saves the current display
        prev_session = context.chat_data['active']
        if prev_session is None:
            # Must have an active session
            reply_msg = await update.message.reply_text(f"No tutorial session right now")

            await update.message.delete()
            await reply_msg.delete()

            return
        context.chat_data['active'] = None
        context.chat_data['current_display'] = None

        # Award those who participated in previous session
        participants = ((q_answered, user) for (q_answered, user) in prev_session.questions.items() if user is not None)
        freqs = context.chat_data['volunteer_freqs']
        log = context.chat_data['logs']
        for q, p in participants:
            freqs[p] = freqs.get(p, 0) + 1

            # Update logs
            if p not in log:
                log[p] = {}
            log[p][prev_session.name] = q

        # Update the options listening filter
        global current_filter
        current_filter.msg_filter = {}

        await update.message.reply_text(f"Tutorial {prev_session.tut_num} has ended",
                                        reply_markup=ReplyKeyboardRemove())

        # Unpin and delete previous display
        # prev_display = context.chat_data.get('current_display', None)
        # if prev_display:
        #     await prev_display.unpin()
        #     await prev_display.delete()
        # context.chat_data['current_display'] = None

        await update.message.delete()
        # await reply_msg.delete()


async def attempt_question(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Runs when the bot receives someone wanting to attempt a question on the active session
    """
    if not (update.message.reply_to_message and
            update.message.reply_to_message == context.chat_data.get('current_display', None)):
        # Must be a reply to the current display of tutorial questions
        return
    current_tut = context.chat_data.get('active', None)
    if current_tut is None:
        # Must have an active session
        reply_msg = await update.message.reply_text(f"No tutorial session right now")

        await update.message.delete()
        await reply_msg.delete()
        return

    qn_num = update.message.text
    if qn_num == "Remove":
        await remove_attempt(update, context)
        return

    # Invalid question number
    if qn_num not in current_tut.questions:
        reply_msg = await update.message.reply.text(f"Invalid question number.")
        await reply_msg.delete()
        await update.message.delete()
        return

    current_attempt = context.user_data.get('attempting', None)
    if current_attempt is not None and current_attempt[0] == current_tut.tut_num:
        reply_msg = await update.message.reply_text(f"You have already attempted a question!")
    elif current_tut.questions[qn_num] is not None:
        reply_msg = await update.message.reply_text(f"Someone has already taken that question. Please try another one")
    else:
        current_tut.questions[qn_num] = update.message.from_user
        context.user_data['attempting'] = current_tut.tut_num, qn_num

        # Update the options listening filter
        global current_filter
        current_filter.msg_filter = current_tut.questions
        reply_msg = await update.message.reply_text(f"Successfully picked question {qn_num}!")

        await display(current_tut, update, context)

    await reply_msg.delete()
    await update.message.delete()


async def remove_attempt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Removes the person's attempt for the active tutorial session
    """
    current_tut = context.chat_data['active']
    person_to_remove = update.message.from_user
    current_attempt = context.user_data.get('attempting', None)
    if current_attempt is None or current_attempt[0] != current_tut.tut_num:
        reply_msg = await update.message.reply_text(f"You have not picked a question")
    else:
        current_tut.questions[current_attempt[1]] = None
        context.user_data['attempting'] = None

        # Update the options listening filter
        global current_filter
        current_filter.msg_filter = current_tut.questions

        reply_msg = await update.message.reply_text("Removed your name successfully")
        await display(current_tut, update, context)

    await update.message.delete()
    await reply_msg.delete()


async def show_attempts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    TODO: Make the dictionary look nicer
    Sends a PM to the user containing a dictionary tracking how many times each person attempted a question.
    Admin-only command
    """
    if await check_if_admin(update):
        # print(context.chat_data['volunteer_freqs'])
        # pprint(context.chat_data['logs'])
        user_to_dm_id = update.message.from_user.id
        chat_name = update.message.chat.title
        freqs = dict(sorted(context.chat_data['volunteer_freqs'].items(), key=lambda x: x[1], reverse=True))
        logs = context.chat_data['logs']
        
        intro_str = f"Here's the attempts for chat {chat_name}"
        await context.bot.send_message(chat_id=user_to_dm_id, text=intro_str)

        # Overall freqs
        builder = io.StringIO()
        builder.write("Overall volunteering frequencies\n\n")
        for k,v in freqs.items():
            builder.write(f"{k.username}: {v}\n")
        await context.bot.send_message(chat_id=user_to_dm_id, text=builder.getvalue())

        # Specific details on who did what qn
        builder = io.StringIO()
        builder.write("Detailed volunteering logs\n\n")
        for k,v in logs.items():
            builder.write(f"{k.username}: ")
            strs = []
            for t,q in v.items():
                strs.append(f"Q{q} in {t}")
            builder.write(", ".join(strs))
            builder.write("\n")
                
        await context.bot.send_message(chat_id=user_to_dm_id, text=builder.getvalue())
    await update.message.delete()

async def help_student(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Prints a how to use the bot for students
    """
    message = """To volunteer to do a question in the upcoming tutorial, just reply to the pinned message made by me with the question you want to do!\n
You can tap on the keyboard icon (next to the paperclip icon) to choose a question.\n
Do note that you can only do one question per tutorial (to give others a chance to answer!).\n
If you wish to remove your name if you already volunteered, just reply to the pinned message again and select the 'Remove' option.\n
If you wish to change the question you want to do, remove your name and then reselect the new question.\n
Keep in mind that I will award class participation to those who volunteer!"""
    await update.message.reply_text(message)

async def help_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Prints a how to for admins
    """
    if await check_if_admin(update):
        builder = io.StringIO()
        builder.write("Admin-only commands\n\n")
        builder.write("/start - Initializes the bot (if it isn't already)\n")
        builder.write("/new <tut number> <*question numbers> - Creates and begins a new tutorial session, sets up a board for students to volunteer questions\n")
        builder.write("/end - Ends currently running tutorial session\n")
        builder.write("/show_attempts - PMs a set of messages containing each students volunteering frequencies and logs of which questions they did\n")                      
        await update.message.reply_text(builder.getvalue())


async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    For handling unknown commands
    """
    reply_msg = await context.bot.send_message(chat_id=update.effective_chat.id,
                                               text="Sorry, I didn't understand that command.")
    await update.message.delete()
    await reply_msg.delete()


def main() -> None:
    """Run the bot."""
    # Create the Application and pass it your bot's token.
    persistence = PicklePersistence(filepath=SAVE_FILE)
    application = Application.builder().token(TOKEN).persistence(
        persistence).build()

    start_handler = CommandHandler("start", start)
    application.add_handler(start_handler)

    new_handler = CommandHandler("new", new_tut)
    application.add_handler(new_handler)

    end_handler = CommandHandler("end", end_tut)
    application.add_handler(end_handler)

    current_filter = QuestionFilter({})
    attempt_handler = MessageHandler(current_filter, attempt_question)
    application.add_handler(attempt_handler)

    show_attempts_handler = CommandHandler("show_attempts", show_attempts)
    application.add_handler(show_attempts_handler)

    help_student_handler = CommandHandler("help", help_student)
    application.add_handler(help_student_handler)

    help_admin_handler = CommandHandler("helpmin", help_admin)
    application.add_handler(help_admin_handler)

    # For unknown commands, must be added last
    unknown_handler = MessageHandler(filters.COMMAND, unknown)
    application.add_handler(unknown_handler)

    # Run the bot until the user presses Ctrl-C
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
