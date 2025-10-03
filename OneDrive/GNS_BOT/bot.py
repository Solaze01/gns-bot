import asyncio
import re
import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from config import *

# Store data
pending_corrections = {}  # {user_id: user_data}
warning_messages = []     # Store warning messages for cleanup
rejoin_attempts = {}      # {user_id: {attempts: int, last_attempt: datetime}}
approved_rejoins = {}     # {user_id: {'display_name': name, 'invite_link': str, 'created_at': datetime}}
member_name_tracker = {}  # {user_id: 'display_name'} - Track all members' names
bot_messages_to_cleanup = []  # Store bot messages for automatic deletion
group_admins = set()      # Store group admin IDs
active_invite_links = {}  # {user_id: {'link': str, 'created_at': datetime, 'used': bool}}

# Get the single group ID
GROUP_ID = list(COURSE_GROUPS["GNS101"].keys())[0]
GROUP_NAME = COURSE_GROUPS["GNS101"][GROUP_ID]
COURSE_CODE = "GNS101"
MODE = COURSE_MODES[COURSE_CODE]

# Auto-delete timing (in seconds)
WARNING_DELETE_TIME = 300      # 5 minutes for warning messages
REMOVAL_DELETE_TIME = 600      # 10 minutes for removal messages
WELCOME_DELETE_TIME = 300      # 5 minutes for welcome messages
CORRECTION_DELETE_TIME = 300   # 5 minutes for correction messages

# Invite link settings
INVITE_LINK_DURATION = 2 * 60 * 60  # 2 hours in seconds (increased from 1 hour)
INVITE_LINK_USES = 1                # Single use

def validate_name(name):
    """Validate name based on current mode"""
    print(f"🔍 Validating name: '{name}' with mode: {MODE}")
    
    if MODE == "pre_matric":
        pattern = r"^[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\s+[A-Z]+\s+\d{12}$"
        result = bool(re.match(pattern, name))
        print(f"   Pre-matric validation result: {result}")
        return result
    else:
        pattern = r"^(?:[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\s+)?[A-Z]{2,4}/\d{2}/\d{4}$"
        result = bool(re.match(pattern, name))
        print(f"   Post-matric validation result: {result}")
        return result

async def is_lecturer_or_admin(user_id: int, context: ContextTypes.DEFAULT_TYPE, chat_id: int = None):
    """Check if user is lecturer or group admin"""
    if chat_id is None:
        chat_id = GROUP_ID
    
    # Check if user is in lecturer IDs
    if user_id in LECTURER_IDS:
        return True
    
    # Check if user is group admin
    if user_id in group_admins:
        return True
    
    # Dynamically check admin status (fallback)
    try:
        chat_member = await context.bot.get_chat_member(chat_id, user_id)
        if chat_member.status in ['creator', 'administrator']:
            group_admins.add(user_id)  # Cache for future checks
            return True
    except Exception as e:
        print(f"⚠️ Error checking admin status for {user_id}: {e}")
    
    return False

async def refresh_admins(context: ContextTypes.DEFAULT_TYPE):
    """Refresh the list of group admins"""
    try:
        admins = await context.bot.get_chat_administrators(GROUP_ID)
        admin_ids = {admin.user.id for admin in admins}
        group_admins.clear()
        group_admins.update(admin_ids)
        print(f"👨‍🏫 Refreshed admin list: {len(group_admins)} admins")
        return True
    except Exception as e:
        print(f"❌ Error refreshing admins: {e}")
        return False

def get_validation_rules():
    """Get validation rules for the course"""
    if MODE == "pre_matric":
        return (
            f"<b>📝 {COURSE_CODE} - NAME FORMAT REQUIRED:</b>\n"
            "• Your Name + JAMB Registration No + Department\n\n"
            "<b>✅ CORRECT EXAMPLES:</b>\n"
            "• Tope Chinedu Garba IFT 202311038308\n"
            "• Tope IFT 202311038308\n\n"
            "<b>❌ INCORRECT EXAMPLES:</b>\n"
            "• Big Mac\n"
            "• Tope Chinedu\n"
            "• Tope Chinedu Garba\n"
            "• IFT 202311038308"
        )
    else:
        return (
            f"📝 {COURSE_CODE} - NAME FORMAT REQUIRED:\n"
            "• Your Matric No OR Name + Matric No\n\n"
            "✅ CORRECT EXAMPLES:\n"
            "• ABC/23/4567\n"
            "• Abdul ABC/23/4567\n\n"
            "❌ INCORRECT EXAMPLES:\n"
            "• Big Mac\n"
            "• Tope Chinedu\n"
            "• Tope Chinedu Garba\n"
            "• (IFT) 202311038308\n"
            "• Tope Chinedu (IFT) 202311038308\n"
            "• Tope Chinedu Abdul APC/23/2145"
        )

# ===== IMPROVED INVITE LINK MANAGEMENT =====
async def create_invite_link(context: ContextTypes.DEFAULT_TYPE, user_id: int, display_name: str):
    """Create a fresh invite link with better error handling"""
    try:
        # Clean up any existing expired links for this user
        if user_id in active_invite_links:
            old_link_data = active_invite_links[user_id]
            # Check if old link is expired
            if datetime.datetime.now() - old_link_data['created_at'] > datetime.timedelta(hours=2):
                del active_invite_links[user_id]
                print(f"🧹 Cleaned up expired link for user {user_id}")
        
        # Create new invite link with longer duration
        invite_link = await context.bot.create_chat_invite_link(
            chat_id=GROUP_ID,
            expire_date=datetime.datetime.now() + datetime.timedelta(seconds=INVITE_LINK_DURATION),
            member_limit=INVITE_LINK_USES,
            name=f"GNS_Rejoin_{user_id}_{datetime.datetime.now().strftime('%H%M%S')}"
        )
        
        # Store the link information
        active_invite_links[user_id] = {
            'link': invite_link.invite_link,
            'created_at': datetime.datetime.now(),
            'used': False,
            'expires_at': datetime.datetime.now() + datetime.timedelta(seconds=INVITE_LINK_DURATION)
        }
        
        print(f"🔗 Created new invite link for {display_name}")
        print(f"   Link expires at: {active_invite_links[user_id]['expires_at'].strftime('%H:%M:%S')}")
        
        return invite_link.invite_link
        
    except Exception as e:
        print(f"❌ Error creating invite link for {user_id}: {e}")
        return None

async def check_invite_link_status(user_id: int):
    """Check if a user's invite link is still valid"""
    if user_id not in active_invite_links:
        return "not_found"
    
    link_data = active_invite_links[user_id]
    current_time = datetime.datetime.now()
    
    # Check if link is expired
    if current_time > link_data['expires_at']:
        del active_invite_links[user_id]
        return "expired"
    
    # Check if link has been used
    if link_data['used']:
        return "used"
    
    return "active"

async def mark_invite_link_used(user_id: int):
    """Mark an invite link as used"""
    if user_id in active_invite_links:
        active_invite_links[user_id]['used'] = True
        print(f"🔗 Marked invite link as used for user {user_id}")

async def cleanup_expired_links(context: ContextTypes.DEFAULT_TYPE):
    """Clean up expired invite links periodically"""
    print("🧹 Cleaning up expired invite links...")
    current_time = datetime.datetime.now()
    expired_links = []
    
    for user_id, link_data in list(active_invite_links.items()):
        if current_time > link_data['expires_at']:
            expired_links.append(user_id)
    
    for user_id in expired_links:
        del active_invite_links[user_id]
        print(f"🧹 Removed expired invite link for user {user_id}")
    
    if expired_links:
        print(f"🧹 Cleaned up {len(expired_links)} expired invite links")

# ===== ENHANCED NAME CHANGE DETECTION =====
async def detect_name_change(user_id: int, current_name: str, context: ContextTypes.DEFAULT_TYPE):
    """
    Detect if a user's name has changed and validate it.
    Returns True if name changed and is invalid, False otherwise.
    """
    # Skip if user is lecturer or admin
    if await is_lecturer_or_admin(user_id, context):
        return False
        
    # Check if we have previous record of this user
    if user_id in member_name_tracker:
        previous_name = member_name_tracker[user_id]
        
        # If name hasn't changed, just validate and return
        if previous_name == current_name:
            if not validate_name(current_name) and user_id not in pending_corrections:
                # Name was always invalid but not yet warned
                await warn_and_schedule_removal(context, GROUP_ID, context._bot.get_chat(GROUP_ID))
            return False
            
        # Name has changed!
        print(f"🔄 Name change detected for user {user_id}: '{previous_name}' → '{current_name}'")
        
        # Update tracker with new name
        member_name_tracker[user_id] = current_name
        
        # Validate the new name
        if validate_name(current_name):
            # Name changed to valid format - remove from pending if exists
            if user_id in pending_corrections:
                try:
                    await context.bot.delete_message(GROUP_ID, pending_corrections[user_id]['warning_msg_id'])
                    if pending_corrections[user_id]['warning_msg_id'] in warning_messages:
                        warning_messages.remove(pending_corrections[user_id]['warning_msg_id'])
                except:
                    pass
                del pending_corrections[user_id]
                
                # Cancel any pending removal job
                jobs = context.job_queue.get_jobs_by_name(f"removal_{user_id}")
                for job in jobs:
                    job.schedule_removal()
                
                correction_msg = await context.bot.send_message(
                    GROUP_ID,
                    f"✅ <b>Name Corrected</b>\n"
                    f"Thank you for fixing your name: <b>{current_name}</b>",
                    parse_mode='HTML'
                )
                # Schedule this message for deletion
                schedule_message_deletion(context, correction_msg.message_id, CORRECTION_DELETE_TIME)
                print(f"✅ Name corrected via change: {current_name}")
            return False
        else:
            # Name changed to invalid format - warn user
            print(f"🚨 Name changed to invalid format: {current_name}")
            return True
    else:
        # New user we haven't tracked before - add to tracker
        member_name_tracker[user_id] = current_name
        return not validate_name(current_name)

# ===== MESSAGE MANAGEMENT SYSTEM =====
def schedule_message_deletion(context: ContextTypes.DEFAULT_TYPE, message_id: int, delay: int):
    """Schedule a message for automatic deletion"""
    bot_messages_to_cleanup.append(message_id)
    context.job_queue.run_once(
        delete_specific_message,
        when=delay,
        data={"message_id": message_id},
        name=f"delete_msg_{message_id}"
    )

async def delete_specific_message(context: ContextTypes.DEFAULT_TYPE):
    """Delete a specific message by ID"""
    job = context.job
    message_id = job.data["message_id"]
    
    try:
        await context.bot.delete_message(GROUP_ID, message_id)
        if message_id in bot_messages_to_cleanup:
            bot_messages_to_cleanup.remove(message_id)
        print(f"🧹 Deleted auto-cleanup message: {message_id}")
    except Exception as e:
        # Message might already be deleted, remove from tracking
        if message_id in bot_messages_to_cleanup:
            bot_messages_to_cleanup.remove(message_id)
        print(f"⚠️ Could not delete message {message_id}: {e}")

async def cleanup_old_messages(context: ContextTypes.DEFAULT_TYPE):
    """Clean up old bot messages periodically"""
    print("🧹 Running periodic message cleanup...")
    
    # Clean up warning messages that are too old
    messages_to_remove = []
    for msg_id in warning_messages:
        try:
            await context.bot.delete_message(GROUP_ID, msg_id)
            messages_to_remove.append(msg_id)
            print(f"🧹 Cleaned up old warning message: {msg_id}")
        except:
            continue
    
    for msg_id in messages_to_remove:
        if msg_id in warning_messages:
            warning_messages.remove(msg_id)

# ===== INITIAL MEMBER SCAN =====
async def scan_all_existing_members(context: ContextTypes.DEFAULT_TYPE):
    """Scan ALL existing members when bot starts and add them to tracker"""
    print("🚀 INITIAL SCAN: Starting admin refresh and member scan...")

    try:
        # Refresh admin list first
        await refresh_admins(context)
        
        admins = await context.bot.get_chat_administrators(GROUP_ID)
        admin_ids = [admin.user.id for admin in admins]
        print(f"   👨‍🏫 Found {len(admin_ids)} admins: {admin_ids}")

        total_members = await context.bot.get_chat_member_count(GROUP_ID)
        print(f"   👥 Total members in group: {total_members}")

        print("⚠️ Cannot fetch full member list from Telegram. "
              "Bot will validate only NEW members and those who message in chat.")
    except Exception as e:
        print(f"❌ Error in initial member scan: {e}")

# ===== CONTINUOUS NAME MONITORING =====
async def scan_all_members(context: ContextTypes.DEFAULT_TYPE):
    """Scan ALL current group members to validate names periodically."""
    group_id = GROUP_ID
    group_name = GROUP_NAME

    print(f"🔍 Scanning ALL members in {group_name} at {datetime.datetime.now().strftime('%H:%M:%S')}")

    try:
        # Get actual current member count from Telegram
        member_count = await context.bot.get_chat_member_count(group_id)
        print(f"   📊 Total members in group: {member_count}")
    except Exception as e:
        print(f"⚠️ Error getting member count: {e}")
        member_count = 0

    total_checked = 0
    invalid_found = 0

    # Check all members we're tracking
    for user_id, last_known_name in list(member_name_tracker.items()):
        try:
            chat_member = await context.bot.get_chat_member(group_id, user_id)
            user = chat_member.user

            if user.is_bot:
                continue

            total_checked += 1
            current_name = user.full_name

            # Use enhanced name change detection
            name_changed_to_invalid = await detect_name_change(user.id, current_name, context)
            
            if name_changed_to_invalid or (not validate_name(current_name) and user.id not in pending_corrections):
                invalid_found += 1
                print(f"🚨 Invalid name found in scan: {current_name}")
                await warn_and_schedule_removal(context, group_id, user)

        except Exception as e:
            print(f"⚠️ Error scanning user {user_id} ({last_known_name}): {e}")
            # Remove from tracker if user left or can't be accessed
            if "user not found" in str(e).lower() or "chat not found" in str(e).lower():
                del member_name_tracker[user_id]
                print(f"   🗑️ Removed left member from tracker: {last_known_name}")

    print(f"   Members processed: {total_checked}")
    print(f"   Invalid members found: {invalid_found}")
    print(f"   Total members in tracker: {len(member_name_tracker)}")
    
    if invalid_found == 0 and total_checked > 0:
        print("✅ All checked members have valid names!")

async def warn_and_schedule_removal(context: ContextTypes.DEFAULT_TYPE, group_id: int, user):
    """
    Warn a user about invalid name format and schedule removal after timeout if not corrected.
    """
    # Skip if user is lecturer or admin
    if await is_lecturer_or_admin(user.id, context):
        print(f"👨‍🏫 Skipping warning for lecturer/admin: {user.full_name}")
        return
        
    try:
        # Send warning in group
        warning_msg = await context.bot.send_message(
            group_id,
            f"🚨 <b>NAME FORMAT VIOLATION DETECTED</b> 🚨\n\n"
            f"@{user.username or user.id}, your name format is invalid!\n\n"
            f"Current name: <b>{user.full_name}</b>\n"
            f"{get_validation_rules()}\n\n"
            f"⏰ <b>You have {NAME_CORRECTION_TIME//60} MINUTES to fix your name</b>\n"
            f"🔴 Failure to comply will result in automatic removal",
            parse_mode='HTML'
        )
        
        # Add to pending corrections
        pending_corrections[user.id] = {
            'warning_msg_id': warning_msg.message_id,
            'username': user.username,
            'display_name': user.full_name,
            'warning_time': datetime.datetime.now(),
            'timer_end': datetime.datetime.now() + datetime.timedelta(seconds=NAME_CORRECTION_TIME)
        }
        
        warning_messages.append(warning_msg.message_id)
        
        # Schedule warning message for deletion
        schedule_message_deletion(context, warning_msg.message_id, WARNING_DELETE_TIME)

        # Schedule removal job
        context.job_queue.run_once(
            remove_user_if_not_corrected,
            when=NAME_CORRECTION_TIME,
            chat_id=group_id,
            data={"user_id": user.id},
            name=f"removal_{user.id}"
        )
        
        print(f"⚠️ Warning sent to: {user.full_name}")

    except Exception as e:
        print(f"⚠️ Error warning/scheduling removal for {user.id}: {e}")

async def remove_user_if_not_corrected(context: ContextTypes.DEFAULT_TYPE):
    """
    Remove user if they haven't corrected their name by the deadline.
    """
    job = context.job
    group_id = job.chat_id
    user_id = job.data["user_id"]

    # Skip if user is lecturer or admin
    if await is_lecturer_or_admin(user_id, context):
        print(f"👨‍🏫 Skipping removal for lecturer/admin: {user_id}")
        if user_id in pending_corrections:
            del pending_corrections[user_id]
        return

    try:
        chat_member = await context.bot.get_chat_member(group_id, user_id)
        current_name = chat_member.user.full_name
        
        if not validate_name(current_name):
            # Name still invalid - REMOVE
            await context.bot.ban_chat_member(group_id, user_id)
            removal_msg = await context.bot.send_message(
                group_id,
                f"🚫 <b>REMOVED: NAME VIOLATION</b>\n"
                f"User failed to correct name format.\n"
                f"Last name: <b>{current_name}</b>\n"
                f"📱 Message @{(await context.bot.get_me()).username} privately to rejoin",
                parse_mode='HTML'
            )
            # Schedule removal message for deletion
            schedule_message_deletion(context, removal_msg.message_id, REMOVAL_DELETE_TIME)
            print(f"🚫 Removed for name violation: {current_name}")
            
            # Remove from tracker and pending
            if user_id in member_name_tracker:
                del member_name_tracker[user_id]
            if user_id in pending_corrections:
                # Try to delete warning message immediately
                try:
                    await context.bot.delete_message(group_id, pending_corrections[user_id]['warning_msg_id'])
                    if pending_corrections[user_id]['warning_msg_id'] in warning_messages:
                        warning_messages.remove(pending_corrections[user_id]['warning_msg_id'])
                except:
                    pass
                del pending_corrections[user_id]
        else:
            # Name was corrected - remove from pending
            if user_id in pending_corrections:
                del pending_corrections[user_id]
                correction_msg = await context.bot.send_message(
                    group_id,
                    f"✅ <b>Name Corrected</b>\n"
                    f"Thank you for fixing your name: <b>{current_name}</b>",
                    parse_mode='HTML'
                )
                # Schedule correction message for deletion
                schedule_message_deletion(context, correction_msg.message_id, CORRECTION_DELETE_TIME)
                print(f"✅ Name corrected: {current_name}")
                
    except Exception as e:
        print(f"⚠️ Error removing user {user_id}: {e}")
        # User might have left already, remove from pending
        if user_id in pending_corrections:
            del pending_corrections[user_id]

# ===== ENHANCED MESSAGE HANDLER WITH NAME CHANGE DETECTION =====
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all messages with name change detection"""
    user = update.effective_user
    if user and not user.is_bot:
        # Skip name validation for lecturers/admins in group chat
        if update.effective_chat.type != 'private':
            if await is_lecturer_or_admin(user.id, context):
                return
        
        # Use enhanced name change detection
        name_changed_to_invalid = await detect_name_change(user.id, user.full_name, context)
        
        if name_changed_to_invalid or (not validate_name(user.full_name) and user.id not in pending_corrections):
            await warn_and_schedule_removal(context, update.effective_chat.id, user)

# ===== STRICT MEMBER JOIN HANDLER =====
async def handle_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle new members in the group"""
    chat_id = update.effective_chat.id
    
    if chat_id != GROUP_ID:
        print(f"❌ Message from wrong chat: {chat_id}")
        return
    
    print(f"🆕 New member(s) detected in group")
    
    for member in update.message.new_chat_members:
        user_id = member.id
        display_name = member.full_name
        username = f"@{member.username}" if member.username else display_name
        
        print(f"   Processing new member: {display_name} (ID: {user_id})")
        
        # SKIP LECTURERS/ADMINS
        if await is_lecturer_or_admin(user_id, context):
            print(f"👨‍🏫 Lecturer/Admin joined: {display_name}")
            # But still add to tracker for monitoring
            member_name_tracker[user_id] = display_name
            continue
            
        if member.id == context.bot.id:
            print(f"🤖 Bot itself joined")
            continue
        
        # Add ALL new members to tracker immediately
        member_name_tracker[user_id] = display_name
        print(f"   📝 Added to member tracker: {display_name}")
        
        # Check if this is an approved rejoin AND name still matches
        if user_id in approved_rejoins:
            approved_info = approved_rejoins[user_id]
            approved_name = approved_info['display_name']
            
            # 🚨 CRITICAL SECURITY CHECK: Verify name hasn't changed
            if display_name == approved_name:
                # Name still matches - allow entry and add to tracker
                member_name_tracker[user_id] = display_name
                del approved_rejoins[user_id]
                
                # Mark the invite link as used
                await mark_invite_link_used(user_id)
                
                welcome_msg = await update.message.reply_text(
                    f"👋 Welcome back to {COURSE_CODE}, <b>{display_name}</b>!\n"
                    f"✅ Auto-approved rejoin with correct name format.",
                    parse_mode='HTML'
                )
                # Schedule welcome message for deletion
                schedule_message_deletion(context, welcome_msg.message_id, WELCOME_DELETE_TIME)
                print(f"✅ Auto-approved rejoin: {display_name}")
                continue
            else:
                # 🚨 SECURITY BREACH: Name changed after approval - REMOVE IMMEDIATELY
                del approved_rejoins[user_id]
                try:
                    await context.bot.ban_chat_member(GROUP_ID, user_id)
                    security_msg = await update.message.reply_text(
                        f"🚫 <b>SECURITY VIOLATION DETECTED</b>\n"
                        f"User changed name after approval!\n"
                        f"Approved as: <b>{approved_name}</b>\n"
                        f"Joined as: <b>{display_name}</b>\n"
                        f"❌ Immediate removal enforced.",
                        parse_mode='HTML'
                    )
                    # Schedule security message for deletion
                    schedule_message_deletion(context, security_msg.message_id, REMOVAL_DELETE_TIME)
                    print(f"🚫 SECURITY REMOVAL: {user_id} changed name from '{approved_name}' to '{display_name}'")
                    continue
                except Exception as e:
                    print(f"❌ Error in security removal: {e}")
        
        # Normal validation for new members
        print(f"   Validating new member name: '{display_name}'")
        if validate_name(display_name):
            # Already added to tracker above - just confirm
            print(f"✅ Approved new member: {display_name}")
            welcome_msg = await update.message.reply_text(
                f"👋 Welcome to {COURSE_CODE}, <b>{display_name}</b>!\n"
                f"Please read all pinned messages and group rules.",
                parse_mode='HTML'
            )
            # Schedule welcome message for deletion
            schedule_message_deletion(context, welcome_msg.message_id, WELCOME_DELETE_TIME)
        else:
            print(f"❌ Invalid name for new member: {display_name}")
            # Member is already in tracker from above - now send warning
            rules = get_validation_rules()
            try:
                warning_msg = await update.message.reply_text(
                    f"🚨 <b>{COURSE_CODE} - STRICT WARNING</b> 🚨\n\n"
                    f"{username}, your name format is INCORRECT!\n\n"
                    f"{rules}\n\n"
                    f"⏰ <b>You have {NAME_CORRECTION_TIME//60} MINUTES to fix this</b>\n"
                    f"⏳ Timer ends at: {(datetime.datetime.now() + datetime.timedelta(seconds=NAME_CORRECTION_TIME)).strftime('%H:%M:%S')}\n"
                    f"🔴 After removal, message me privately to rejoin automatically",
                    parse_mode='HTML'
                )
                
                pending_corrections[user_id] = {
                    'warning_msg_id': warning_msg.message_id,
                    'username': member.username,
                    'display_name': display_name,
                    'warning_time': datetime.datetime.now(),
                    'timer_end': datetime.datetime.now() + datetime.timedelta(seconds=NAME_CORRECTION_TIME)
                }
                
                warning_messages.append(warning_msg.message_id)
                
                # Schedule warning message for deletion
                schedule_message_deletion(context, warning_msg.message_id, WARNING_DELETE_TIME)
                
                print(f"⚠️ Warning sent to: {display_name}")
                
            except Exception as e:
                print(f"❌ Error sending warning message: {e}")

# ===== IMPROVED PRIVATE MESSAGE HANDLER =====
async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle private messages - ONLY for rejoin requests from regular users"""
    user_id = update.effective_user.id
    display_name = update.effective_user.full_name
    
    # 🚨 CRITICAL FIX: Skip all rejoin logic for lecturers/admins
    if await is_lecturer_or_admin(user_id, context):
        print(f"👑 Admin/Lecturer messaged privately: {display_name}")
        
        # Check if it's a command that should work in private
        message_text = update.message.text or ""
        if message_text.startswith('/'):
            # Let command handlers process this instead
            return
        
        # Only show the admin welcome for non-command messages
        await update.message.reply_text(
            f"👋 <b>Hello Admin/Lecturer!</b>\n\n"
            f"You don't need to use the rejoin system.\n"
            f"You can use these commands in the group:\n"
            f"• /announce - Make announcements\n"
            f"• /switch_mode - Change name validation mode\n"
            f"• /status - Check bot status\n\n"
            f"Your name format is not restricted.",
            parse_mode='HTML'
        )
        return
    
    # ===== REGULAR USER REJOIN LOGIC =====
    # Initialize rejoin attempts tracking (only for regular users)
    if user_id not in rejoin_attempts:
        rejoin_attempts[user_id] = {'attempts': 0, 'last_attempt': None}
    
    now = datetime.datetime.now()
    user_data = rejoin_attempts[user_id]
    
    # Check cooldown (only for regular users)
    if user_data['last_attempt']:
        time_since_last = (now - user_data['last_attempt']).total_seconds()
        if time_since_last < REJOIN_COOLDOWN:
            remaining = (REJOIN_COOLDOWN - time_since_last) // 60
            await update.message.reply_text(
                f"⏳ <b>Cooldown Active</b>\n"
                f"Please wait {int(remaining)} minutes before trying again.\n"
                f"Use this time to fix your name format properly.",
                parse_mode='HTML'
            )
            return
    
    # Check attempt limit (only for regular users)
    if user_data['attempts'] >= REJOIN_ATTEMPTS_LIMIT:
        await update.message.reply_text(
            f"🚫 <b>Maximum Attempts Reached</b>\n"
            f"You have used all {REJOIN_ATTEMPTS_LIMIT} rejoin attempts.\n"
            f"Please contact a lecturer directly for assistance.",
            parse_mode='HTML'
        )
        return
    
    print(f"🔄 Rejoin request from: {display_name} (Attempt: {user_data['attempts'] + 1})")
    
    # Check if name is valid
    if validate_name(display_name):
        # ✅ Name is correct - AUTO APPROVE
        user_data['attempts'] += 1
        user_data['last_attempt'] = now
        
        # Store approval info with the EXACT name that was validated
        approved_rejoins[user_id] = {
            'display_name': display_name,  # Store the exact validated name
            'approval_time': now
        }
        
        try:
            # Create a fresh invite link with improved settings
            invite_link = await create_invite_link(context, user_id, display_name)
            
            if not invite_link:
                await update.message.reply_text(
                    "❌ <b>Error creating invite link</b>\n"
                    "Please try again in a few moments or contact a lecturer for assistance.",
                    parse_mode='HTML'
                )
                return
            
            # Calculate expiration time for user information
            expires_at = datetime.datetime.now() + datetime.timedelta(seconds=INVITE_LINK_DURATION)
            expires_in_hours = INVITE_LINK_DURATION // 3600
            
            await update.message.reply_text(
                f"✅ <b>AUTO-APPROVED!</b>\n\n"
                f"Your name format is correct: <b>{display_name}</b>\n"
                f"Rejoin attempt: {user_data['attempts']}/{REJOIN_ATTEMPTS_LIMIT}\n\n"
                f"<b>🔗 Invite Link (Valid for {expires_in_hours} hours, Single Use):</b>\n"
                f"<code>{invite_link}</code>\n\n"
                f"<b>⏰ Link expires at:</b> {expires_at.strftime('%H:%M:%S')}\n\n"
                f"<b>🚨 IMPORTANT:</b>\n"
                f"• Click the link IMMEDIATELY\n"
                f"• Link works only ONCE\n"
                f"• Expires in {expires_in_hours} hours\n"
                f"• Do not change your name after joining\n"
                f"• Read group rules after joining",
                parse_mode='HTML'
            )
            print(f"✅ Auto-approved rejoin for: {display_name}")
            
            # Notify lecturer
            for lecturer_id in LECTURER_IDS:
                try:
                    await context.bot.send_message(
                        lecturer_id,
                        f"🤖 <b>Bot Auto-Approved Rejoin</b>\n"
                        f"User: {display_name} (@{update.effective_user.username})\n"
                        f"ID: {user_id}\n"
                        f"Course: {COURSE_CODE}\n"
                        f"Attempt: {user_data['attempts']}/{REJOIN_ATTEMPTS_LIMIT}\n"
                        f"Time: {now.strftime('%Y-%m-%d %H:%M:%S')}",
                        parse_mode='HTML'
                    )
                except:
                    pass
                    
        except Exception as e:
            print(f"Error creating invite: {e}")
            # Remove from approved if link creation failed
            if user_id in approved_rejoins:
                del approved_rejoins[user_id]
            await update.message.reply_text(
                "❌ <b>Error creating invite link</b>\n"
                "Please contact a lecturer for manual assistance.",
                parse_mode='HTML'
            )
    else:
        # ❌ Name doesn't match format
        user_data['attempts'] += 1
        user_data['last_attempt'] = now
        
        remaining_attempts = REJOIN_ATTEMPTS_LIMIT - user_data['attempts']
        
        rules = get_validation_rules()
        await update.message.reply_text(
            f"❌ <b>Name Doesn't Match Required Format</b>\n\n"
            f"Current name: <b>{display_name}</b>\n\n"
            f"{rules}\n"
            f"<b>📊 Attempts:</b> {user_data['attempts']}/{REJOIN_ATTEMPTS_LIMIT}\n"
            f"<b>🔄 Remaining attempts:</b> {remaining_attempts}\n\n"
            f"<b>⚠️ Instructions:</b>\n"
            f"1. Change your Telegram display name to correct format\n"
            f"2. Wait for the change to update\n"
            f"3. Message me again to recheck\n"
            f"4. You'll get automatic invite link if correct",
            parse_mode='HTML'
        )
        print(f"❌ Rejoin denied - incorrect name: {display_name}")

# ===== SIMPLIFIED LECTURER/ADMIN COMMANDS =====
async def switch_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Switch mode for the course - Lecturers/Admins only"""
    user_id = update.effective_user.id
    
    # Check if user is lecturer or admin
    if not await is_lecturer_or_admin(user_id, context):
        await update.message.reply_text(
            "❌ <b>Access Denied</b>\n"
            "Only lecturers and group admins can use this command.",
            parse_mode='HTML'
        )
        return
    
    if not context.args:
        await update.message.reply_text(
            "🔄 <b>Switch Name Validation Mode</b>\n\n"
            "Usage: <code>/switch_mode mode</code>\n\n"
            "Available modes:\n"
            "• <code>pre_matric</code> - For JAMB registration numbers\n"
            "• <code>post_matric</code> - For matriculation numbers\n\n"
            "Example: <code>/switch_mode pre_matric</code>",
            parse_mode='HTML'
        )
        return
    
    new_mode = context.args[0].lower()
    
    if new_mode not in ["pre_matric", "post_matric"]:
        await update.message.reply_text(
            "❌ <b>Invalid Mode</b>\n\n"
            "Please use:\n"
            "• <code>pre_matric</code> or\n"
            "• <code>post_matric</code>\n\n"
            "Example: <code>/switch_mode pre_matric</code>",
            parse_mode='HTML'
        )
        return
    
    global MODE
    MODE = new_mode
    COURSE_MODES[COURSE_CODE] = new_mode
    
    await update.message.reply_text(
        f"✅ <b>Mode Changed Successfully!</b>\n\n"
        f"Course: <b>{COURSE_CODE}</b>\n"
        f"New Mode: <b>{new_mode}</b>\n\n"
        f"<b>New Validation Rules:</b>\n"
        f"{get_validation_rules()}",
        parse_mode='HTML'
    )
    print(f"🔧 Mode switched to {new_mode} by user {user_id}")

async def announce(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Announce to the group - Lecturers/Admins only - SIMPLIFIED"""
    user_id = update.effective_user.id
    
    # Check if user is lecturer or admin
    if not await is_lecturer_or_admin(user_id, context):
        await update.message.reply_text(
            "❌ <b>Access Denied</b>\n"
            "Only lecturers and group admins can use this command.",
            parse_mode='HTML'
        )
        return
    
    if not context.args:
        await update.message.reply_text(
            "📢 <b>Make Announcement</b>\n\n"
            "Usage: <code>/announce your message here</code>\n\n"
            "📝 <b>Examples:</b>\n"
            "• <code>/announce Class is cancelled today</code>\n"
            "• <code>/announce Assignment due next week</code>\n"
            "• <code>/announce Important: Exam schedule changed</code>\n\n"
            "💡 <b>Tip:</b> Just type your announcement after the command!",
            parse_mode='HTML'
        )
        return
    
    message = " ".join(context.args)
    
    try:
        announcement_msg = await context.bot.send_message(
            GROUP_ID, 
            f"📢 <b>{COURSE_CODE} ANNOUNCEMENT</b> 📢\n\n"
            f"{message}\n\n"
            f"<i>Posted by: {update.effective_user.full_name}</i>",
            parse_mode='HTML'
        )
        await update.message.reply_text(
            "✅ <b>Announcement Sent!</b>\n"
            "Your message has been posted to the group.",
            parse_mode='HTML'
        )
        print(f"📢 Announcement sent by {user_id}: {message}")
    except Exception as e:
        print(f"❌ Failed to announce: {e}")
        await update.message.reply_text(
            "❌ <b>Failed to send announcement</b>\n"
            "Please check if the bot has permission to send messages in the group.",
            parse_mode='HTML'
        )

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show bot status - Lecturers/Admins only"""
    user_id = update.effective_user.id
    
    # Check if user is lecturer or admin
    if not await is_lecturer_or_admin(user_id, context):
        await update.message.reply_text(
            "❌ <b>Access Denied</b>\n"
            "Only lecturers and group admins can use this command.",
            parse_mode='HTML'
        )
        return
    
    # Refresh admin list for accurate count
    await refresh_admins(context)
    
    # Count active invite links
    active_links = sum(1 for link_data in active_invite_links.values() 
                      if datetime.datetime.now() < link_data['expires_at'] and not link_data['used'])
    
    status_text = (
        f"🤖 <b>GNS BOT STATUS</b>\n\n"
        f"📚 <b>Course:</b> {COURSE_CODE}\n"
        f"🏫 <b>Group:</b> {GROUP_NAME}\n"
        f"🔧 <b>Mode:</b> {MODE}\n"
        f"👥 <b>Tracked Members:</b> {len(member_name_tracker)}\n"
        f"⚠️ <b>Pending Corrections:</b> {len(pending_corrections)}\n"
        f"🔄 <b>Rejoin Attempts:</b> {len(rejoin_attempts)}\n"
        f"✅ <b>Approved Rejoins:</b> {len(approved_rejoins)}\n"
        f"🔗 <b>Active Invite Links:</b> {active_links}\n"
        f"👑 <b>Group Admins:</b> {len(group_admins)}\n\n"
        f"🟢 <b>System Status:</b> All Systems Operational"
    )
    
    await update.message.reply_text(status_text, parse_mode='HTML')
    print(f"📊 Status checked by user {user_id}")

# ===== START COMMAND =====
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    user_id = update.effective_user.id
    
    if await is_lecturer_or_admin(user_id, context):
        await update.message.reply_text(
            f"👋 <b>Welcome Admin/Lecturer!</b>\n\n"
            f"🎯 <b>Available Commands:</b>\n\n"
            f"📢 <b>Announcements:</b>\n"
            f"• <code>/announce your message</code> - Send message to group\n\n"
            f"⚙️ <b>Settings:</b>\n"
            f"• <code>/switch_mode pre_matric/post_matric</code> - Change name format\n\n"
            f"📊 <b>Information:</b>\n"
            f"• <code>/status</code> - Check bot statistics\n\n"
            f"💡 <b>Note:</b> You are exempt from name format requirements.",
            parse_mode='HTML'
        )
    else:
        await update.message.reply_text(
            f"👋 <b>Welcome to {COURSE_CODE} Verification!</b>\n\n"
            f"📝 <b>Name Format Required:</b>\n"
            f"{get_validation_rules()}\n\n"
            f"🚀 <b>How to Join:</b>\n"
            f"1. Change your Telegram display name\n"
            f"2. Come back here and send any message\n"
            f"3. Get invite link if name is correct\n"
            f"4. Join the group\n\n"
            f"⚠️ <b>Note:</b> Bot monitors name changes continuously",
            parse_mode='HTML'
        )

# ===== ERROR HANDLER =====
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle errors in the bot"""
    print(f"❌ Error occurred: {context.error}")
    
    try:
        # Notify the user about the error
        if update and update.effective_message:
            await update.effective_message.reply_text(
                "❌ An error occurred while processing your request. "
                "Please try again or contact an administrator if the problem persists."
            )
    except Exception as e:
        print(f"❌ Error in error handler: {e}")

# ===== CLEANUP & SETUP =====
async def cleanup_warnings(context: ContextTypes.DEFAULT_TYPE):
    """Clean up warning messages periodically"""
    messages_to_remove = []
    for msg_id in warning_messages:
        try:
            await context.bot.delete_message(GROUP_ID, msg_id)
            messages_to_remove.append(msg_id)
            print(f"🧹 Cleaned up old warning message: {msg_id}")
        except:
            continue
    
    for msg_id in messages_to_remove:
        if msg_id in warning_messages:
            warning_messages.remove(msg_id)

async def check_pending_removals(context: ContextTypes.DEFAULT_TYPE):
    """Check and remove members whose correction time has expired"""
    print("⏰ Checking pending removals...")
    current_time = datetime.datetime.now()
    users_to_remove = []
    
    for user_id, pending_info in list(pending_corrections.items()):
        if current_time >= pending_info['timer_end']:
            users_to_remove.append((user_id, pending_info))
    
    for user_id, pending_info in users_to_remove:
        print(f"🚫 Time expired for: {pending_info['display_name']}")
        try:
            # Get current member info
            chat_member = await context.bot.get_chat_member(GROUP_ID, user_id)
            current_name = chat_member.user.full_name
            
            # Check if name was corrected
            if validate_name(current_name):
                # Name corrected - remove from pending
                del pending_corrections[user_id]
                member_name_tracker[user_id] = current_name
                correction_msg = await context.bot.send_message(
                    GROUP_ID,
                    f"✅ <b>Name Corrected</b>\n"
                    f"Thank you for fixing your name: <b>{current_name}</b>",
                    parse_mode='HTML'
                )
                # Schedule correction message for deletion
                schedule_message_deletion(context, correction_msg.message_id, CORRECTION_DELETE_TIME)
                print(f"✅ Name corrected: {current_name}")
            else:
                # Name still invalid - REMOVE
                del pending_corrections[user_id]
                try:
                    await context.bot.ban_chat_member(GROUP_ID, user_id)
                    removal_msg = await context.bot.send_message(
                        GROUP_ID,
                        f"🚫 <b>REMOVED: NAME VIOLATION</b>\n"
                        f"User failed to correct name format.\n"
                        f"Last name: <b>{current_name}</b>\n"
                        f"📱 Message @{(await context.bot.get_me()).username} privately to rejoin",
                        parse_mode='HTML'
                    )
                    # Schedule removal message for deletion
                    schedule_message_deletion(context, removal_msg.message_id, REMOVAL_DELETE_TIME)
                    print(f"🚫 Removed for name violation: {current_name}")
                    
                    # Remove from tracker
                    if user_id in member_name_tracker:
                        del member_name_tracker[user_id]
                        
                except Exception as e:
                    print(f"❌ Error removing user: {e}")
            
            # Try to delete warning message
            try:
                await context.bot.delete_message(GROUP_ID, pending_info['warning_msg_id'])
                if pending_info['warning_msg_id'] in warning_messages:
                    warning_messages.remove(pending_info['warning_msg_id'])
                print(f"🧹 Cleaned up warning message for {pending_info['display_name']}")
            except Exception as e:
                print(f"   Could not delete warning message: {e}")
                
        except Exception as e:
            print(f"❌ Error processing removal for {user_id}: {e}")
            # User might have left already, remove from pending
            del pending_corrections[user_id]

async def refresh_admins_periodically(context: ContextTypes.DEFAULT_TYPE):
    """Periodically refresh admin list"""
    await refresh_admins(context)

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    
    # Add error handler
    app.add_error_handler(error_handler)
    
    job_queue = app.job_queue
    job_queue.run_repeating(cleanup_warnings, interval=CLEANUP_INTERVAL, first=10)
    
    # Add initial member scan when bot starts
    job_queue.run_once(scan_all_existing_members, when=5)  # 5 seconds after start
    
    # Add continuous monitoring job (every 2 minutes)
    job_queue.run_repeating(scan_all_members, interval=120, first=30)  # 2 minutes
    
    # Add pending removal checker (every minute)
    job_queue.run_repeating(check_pending_removals, interval=60, first=10)  # 1 minute
    
    # Add admin refresh job (every 10 minutes)
    job_queue.run_repeating(refresh_admins_periodically, interval=600, first=60)  # 10 minutes
    
    # Add invite link cleanup job (every 30 minutes)
    job_queue.run_repeating(cleanup_expired_links, interval=1800, first=300)  # 30 minutes
    
    # 🚨 CRITICAL FIX: Command handlers MUST come before message handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("switch_mode", switch_mode))
    app.add_handler(CommandHandler("announce", announce))
    app.add_handler(CommandHandler("status", status))
    
    # Message handlers (with lower priority)
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, handle_new_member))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, handle_private_message))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    print("🤖 GNS BOT STARTED - IMPROVED INVITE LINK SYSTEM")
    print(f"📚 Course: {COURSE_CODE}")
    print(f"🏫 Group: {GROUP_NAME} (ID: {GROUP_ID})")
    print(f"🔧 Mode: {MODE}")
    print(f"👨‍🏫 Lecturers: {LECTURER_IDS}")
    print("🔗 INVITE LINKS: 2-hour duration with better management")
    print("🧹 LINK CLEANUP: Automatic cleanup of expired links")
    print("📊 STATUS: Now shows active invite link count")
    print("🎯 SIMPLIFIED: Easy-to-use commands for lecturers")
    
    app.run_polling()

if __name__ == "__main__":
    main()