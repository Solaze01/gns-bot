# Bot configuration
BOT_TOKEN = "7987731441:AAEyMhUVPbNPO9HGzElFhLu3ymr1WmQVbCU"

# Single GNS101 group for now
COURSE_GROUPS = {
    "GNS101": {
        -1002994965915: "GNS101 Group 1",
    }
}

# Lecturers/Admins (their Telegram user IDs)
LECTURER_IDS = [7394280481]

# Name validation patterns
PRE_MATRIC_PATTERN = r"^[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\s+[A-Z]+\s+\d{12}$"
POST_MATRIC_PATTERN = r"^(?:[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\s+)?[A-Z]{2,4}/\d{2}/\d{4}$"

# Current mode for the course
COURSE_MODES = {
    "GNS101": "pre_matric",  # or "post_matric"
}

# Time settings
NAME_CORRECTION_TIME = 15 * 60  # 15 minutes
CLEANUP_INTERVAL = 6 * 60 * 60  # 6 hours

# Rejoin settings
REJOIN_ATTEMPTS_LIMIT = 3
REJOIN_COOLDOWN = 30 * 60  # 30 minutes