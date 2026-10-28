from flask import Flask, render_template, request, redirect, url_for, session, flash, g, abort
from werkzeug.security import generate_password_hash, check_password_hash
from cryptography.fernet import Fernet
import collections
import json
import math
import sqlite3
import hashlib
import re
from datetime import datetime

app = Flask(__name__)
app.secret_key = '123456789' 
DATABASE = 'database.sqlite'

# Load censorship data
# WARNING! The censorship.dat file contains disturbing language when decrypted. 
# If you want to test whether moderation works, 
# you can trigger censorship using these words: 
# tier1badword, tier2badword, tier3badword
ENCRYPTED_FILE_PATH = 'censorship.dat'
fernet = Fernet('xpplx11wZUibz0E8tV8Z9mf-wwggzSrc21uQ17Qq2gg=')
with open(ENCRYPTED_FILE_PATH, 'rb') as encrypted_file:
    encrypted_data = encrypted_file.read()
decrypted_data = fernet.decrypt(encrypted_data)
MODERATION_CONFIG = json.loads(decrypted_data)
TIER1_WORDS = MODERATION_CONFIG['categories']['tier1_severe_violations']['words']
TIER2_PHRASES = MODERATION_CONFIG['categories']['tier2_spam_scams']['phrases']
TIER3_WORDS = MODERATION_CONFIG['categories']['tier3_mild_profanity']['words']

def get_db():
    """
    Connect to the application's configured database. The connection
    is unique for each request and will be reused if this is called
    again.
    """
    if 'db' not in g:
        g.db = sqlite3.connect(
            DATABASE,
            detect_types=sqlite3.PARSE_DECLTYPES
        )
        g.db.row_factory = sqlite3.Row
        ensure_schema(g.db)

    return g.db


# Databases whose schema has already been checked during this run
_checked_databases = set()


def ensure_schema(db):
    """Adds columns that newer features need but older copies of the database don't have yet."""
    if DATABASE in _checked_databases:
        return
    user_columns = {row['name'] for row in db.execute('PRAGMA table_info(users)')}
    # Interests the user removed themselves, so they are not detected from their posts again
    if 'removed_interests' not in user_columns:
        db.execute("ALTER TABLE users ADD COLUMN removed_interests TEXT DEFAULT ''")
        db.commit()
    # Newcomers who chose "Don't ask again" on the introduction prompt
    if 'intro_skipped' not in user_columns:
        db.execute("ALTER TABLE users ADD COLUMN intro_skipped INTEGER DEFAULT 0")
        db.commit()
    post_columns = {row['name'] for row in db.execute('PRAGMA table_info(posts)')}
    # Marks introduction posts, so existing members can be invited to welcome their authors
    if 'is_introduction' not in post_columns:
        db.execute("ALTER TABLE posts ADD COLUMN is_introduction INTEGER DEFAULT 0")
        # Introductions written with the earlier version of the prompt
        db.execute("UPDATE posts SET is_introduction = 1 WHERE content LIKE 'Hi everyone! I''m new here%'")
        db.commit()
    _checked_databases.add(DATABASE)


@app.teardown_appcontext
def close_connection(exception):
    """Closes the database again at the end of the request."""
    db = g.pop('db', None)

    if db is not None:
        db.close()


def query_db(query, args=(), one=False, commit=False):
    """
    Queries the database and returns a list of dictionaries, a single
    dictionary, or None. Also handles write operations.
    """
    db = get_db()
    
    # Using 'with' on a connection object implicitly handles transactions.
    # The 'with' statement will automatically commit if successful, 
    # or rollback if an exception occurs. This is safer.
    try:
        with db:
            cur = db.execute(query, args)
        
        # For SELECT statements, fetch the results after the transaction block
        if not commit:
            rv = cur.fetchall()
            return (rv[0] if rv else None) if one else rv
        
        # For write operations, we might want the cursor to get info like lastrowid
        return cur

    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return None

@app.template_filter('datetimeformat')
def datetimeformat(value):
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        dt = datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
    else:
        return "N/A"
    return dt.strftime('%b %d, %Y %H:%M')

REACTION_EMOJIS = {
    'like': '❤️', 'love': '😍', 'laugh': '😂',
    'wow': '😮', 'sad': '😢', 'angry': '😠',
}
REACTION_TYPES = list(REACTION_EMOJIS.keys())


def get_account_age_days(user_id):
    """Returns how many full days ago a user's account was created, or None if unknown."""
    row = query_db('SELECT created_at FROM users WHERE id = ?', (user_id,), one=True)
    if not row or not row['created_at']:
        return None
    created_at = row['created_at']
    if isinstance(created_at, str):
        created_at = datetime.strptime(created_at, '%Y-%m-%d %H:%M:%S')
    # SQLite's CURRENT_TIMESTAMP is in UTC, so compare against UTC
    return (datetime.utcnow() - created_at).days


# Accounts younger than this many days count as newcomers
NEWCOMER_DAYS = 7
# Introductions from this many days back are shown to existing members to welcome
WELCOME_WINDOW_DAYS = 14


def is_newcomer_age(account_age_days):
    return account_age_days is not None and account_age_days < NEWCOMER_DAYS


def welcome_suggestion(intro_author, intro_interests, current_user_interests):
    """Suggested welcome comment for an introduction, mentioning an interest both users share if there is one."""
    current_lower = {i.lower() for i in split_interests(current_user_interests)}
    shared = [i for i in split_interests(intro_interests) if i.lower() in current_lower]
    if shared:
        return f"Welcome to the community, {intro_author}! I'm into {shared[0]} too, looking forward to your posts about it."
    return f"Welcome to the community, {intro_author}! Glad to have you here, feel free to ask if you have any questions."


@app.route('/')
def feed():
    #  1. Get Pagination and Filter Parameters 
    try:
        page = int(request.args.get('page', 1))
    except ValueError:
        page = 1
    sort = request.args.get('sort', 'new').lower()
    show = request.args.get('show', 'all').lower()
    
    # Define how many posts to show per page
    POSTS_PER_PAGE = 10
    offset = (page - 1) * POSTS_PER_PAGE

    current_user_id = session.get('user_id')
    params = []

    #  2. Build the Query 
    where_clause = ""
    if show == 'following' and current_user_id:
        where_clause = "WHERE p.user_id IN (SELECT followed_id FROM follows WHERE follower_id = ?)"
        params.append(current_user_id)

    # Add the pagination parameters to the query arguments
    pagination_params = (POSTS_PER_PAGE, offset)

    if sort == 'popular':
        query = f"""
            SELECT p.id, p.content, p.created_at, u.username, u.id as user_id,
                   IFNULL(r.total_reactions, 0) as total_reactions
            FROM posts p
            JOIN users u ON p.user_id = u.id
            LEFT JOIN (
                SELECT post_id, COUNT(*) as total_reactions FROM reactions GROUP BY post_id
            ) r ON p.id = r.post_id
            {where_clause}
            ORDER BY total_reactions DESC, p.created_at DESC
            LIMIT ? OFFSET ?
        """
        final_params = params + list(pagination_params)
        posts = query_db(query, final_params)
    elif sort == 'recommended':
        posts = recommend(current_user_id, show == 'following' and current_user_id)
    elif sort == 'needed':
        # Only posts that have no comments yet, newest first
        no_comments = "NOT EXISTS (SELECT 1 FROM comments c WHERE c.post_id = p.id)"
        needed_where = f"{where_clause} AND {no_comments}" if where_clause else f"WHERE {no_comments}"
        query = f"""
            SELECT p.id, p.content, p.created_at, u.username, u.id as user_id
            FROM posts p
            JOIN users u ON p.user_id = u.id
            {needed_where}
            ORDER BY p.created_at DESC
            LIMIT ? OFFSET ?
        """
        final_params = params + list(pagination_params)
        posts = query_db(query, final_params)
    else:  # Default sort is 'new'
        query = f"""
            SELECT p.id, p.content, p.created_at, u.username, u.id as user_id
            FROM posts p
            JOIN users u ON p.user_id = u.id
            {where_clause}
            ORDER BY p.created_at DESC
            LIMIT ? OFFSET ?
        """
        final_params = params + list(pagination_params)
        posts = query_db(query, final_params)

    posts_data = []
    for post in posts:
        # Determine if the current user follows the poster
        followed_poster = False
        if current_user_id and post['user_id'] != current_user_id:
            follow_check = query_db(
                'SELECT 1 FROM follows WHERE follower_id = ? AND followed_id = ?',
                (current_user_id, post['user_id']),
                one=True
            )
            if follow_check:
                followed_poster = True

        # Determine if the current user reacted to this post and with what reaction
        user_reaction = None
        if current_user_id:
            reaction_check = query_db(
                'SELECT reaction_type FROM reactions WHERE user_id = ? AND post_id = ?',
                (current_user_id, post['id']),
                one=True
            )
            if reaction_check:
                user_reaction = reaction_check['reaction_type']

        reactions = query_db('SELECT reaction_type, COUNT(*) as count FROM reactions WHERE post_id = ? GROUP BY reaction_type', (post['id'],))
        comments_raw = query_db('SELECT c.id, c.content, c.created_at, u.username, u.id as user_id FROM comments c JOIN users u ON c.user_id = u.id WHERE c.post_id = ? ORDER BY c.created_at ASC', (post['id'],))
        post_dict = dict(post)
        post_dict['content'], _ = moderate_content(post_dict['content'])
        comments_moderated = []
        for comment in comments_raw:
            comment_dict = dict(comment)
            comment_dict['content'], _ = moderate_content(comment_dict['content'])
            comments_moderated.append(comment_dict)

        # Check if the post author is a newcomer (account less than NEWCOMER_DAYS old)
        account_age_days = get_account_age_days(post['user_id'])
        is_newcomer = is_newcomer_age(account_age_days)

        # Introductions show how many other members have welcomed their author
        intro = query_db('SELECT is_introduction FROM posts WHERE id = ?', (post['id'],), one=True)
        post_dict['is_introduction'] = bool(intro and intro['is_introduction'])
        post_dict['welcome_count'] = len({c['user_id'] for c in comments_raw if c['user_id'] != post['user_id']})

        posts_data.append({
            'post': post_dict,
            'reactions': reactions,
            'user_reaction': user_reaction,
            'followed_poster': followed_poster,
            'comments': comments_moderated,
            'account_age_days': account_age_days, # Author's account age, for rendering
            'is_newcomer': is_newcomer # Pass newcomer status to Jinja2
        })

    # Calculate Weekly Progress and Goal for the Current User
    current_user_id = session.get('user_id')
    weekly_progress = 0
    weekly_goal = 5
    
    if current_user_id:
        #Retrieve the user's weekly goal from the database
        user_row = query_db('SELECT weekly_goal FROM users WHERE id = ?', (current_user_id,), one=True)
        if user_row and user_row['weekly_goal']:
            weekly_goal = user_row['weekly_goal']

        #Calculate the user's weekly progress based on their posts in the last 7 days
        posts_count = query_db('SELECT COUNT(*) as count FROM posts WHERE user_id = ? AND created_at >= datetime("now", "-7 days")', (current_user_id,), one=True)['count']
        comments_count = query_db('SELECT COUNT(*) as count FROM comments WHERE user_id = ? AND created_at >= datetime("now", "-7 days")', (current_user_id,), one=True)['count']
        
        weekly_progress = posts_count + comments_count

    # Introduction Prompt (Design Claim 19): newcomers who haven't introduced themselves yet get a guided
    # introduction form, unless they chose "Don't ask again", or "Not now" during this visit
    is_current_user_newcomer = False
    show_intro_prompt = False
    intro_defaults = {}
    welcome_queue = []
    if current_user_id:
        current_user = query_db('SELECT username, location, interests, intro_skipped FROM users WHERE id = ?',
                                (current_user_id,), one=True)
        is_current_user_newcomer = is_newcomer_age(get_account_age_days(current_user_id))
        if is_current_user_newcomer and not current_user['intro_skipped'] and not session.get('intro_snoozed'):
            has_introduced = query_db('SELECT 1 FROM posts WHERE user_id = ? AND is_introduction = 1 LIMIT 1',
                                      (current_user_id,), one=True)
            show_intro_prompt = not has_introduced
            # Pre-fill the form from the profile, or with what the user typed if their introduction was blocked
            intro_defaults = session.pop('intro_form', None) or {
                'location': current_user['location'] or '',
                'interests': current_user['interests'] or '',
                'about': '',
            }

        # Welcome New Members: recent introductions this user hasn't replied to yet,
        # the ones with the fewest welcomes first so no newcomer is left without a reply
        intros = query_db(f'''
            SELECT p.id, p.content, p.created_at, u.username, u.id AS user_id, u.interests,
                   (SELECT COUNT(DISTINCT c.user_id) FROM comments c
                    WHERE c.post_id = p.id AND c.user_id != p.user_id) AS welcome_count
            FROM posts p JOIN users u ON p.user_id = u.id
            WHERE p.is_introduction = 1 AND p.user_id != ?
              AND p.created_at >= datetime('now', '-{WELCOME_WINDOW_DAYS} days')
              AND NOT EXISTS (SELECT 1 FROM comments c WHERE c.post_id = p.id AND c.user_id = ?)
            ORDER BY welcome_count ASC, p.created_at DESC
            LIMIT 3''', (current_user_id, current_user_id))
        for intro in intros:
            intro = dict(intro)
            intro['content'], _ = moderate_content(intro['content'])
            intro['suggestion'] = welcome_suggestion(intro['username'], intro['interests'], current_user['interests'])
            welcome_queue.append(intro)

    #  4. Render Template with Pagination Info
    return render_template('feed.html.j2', 
                           posts=posts_data, 
                           current_sort=sort,
                           current_show=show,
                           page=page, # Pass current page number
                           per_page=POSTS_PER_PAGE, # Pass items per page
                           reaction_emojis=REACTION_EMOJIS,
                           reaction_types=REACTION_TYPES,
                           weekly_progress=weekly_progress, # Pass the user's weekly progress
                           weekly_goal=weekly_goal, # Pass the user's weekly goal
                           is_current_user_newcomer=is_current_user_newcomer, # Account is less than 7 days old
                           show_intro_prompt=show_intro_prompt, # Newcomer who hasn't introduced themselves yet
                           intro_defaults=intro_defaults, # Pre-filled answers for the introduction form
                           welcome_queue=welcome_queue # Introductions the current user can welcome
                           )


@app.route('/introduce', methods=['POST'])
def introduce():
    """Introduction Prompt (Design Claim 19): builds an introduction post from the guided form."""
    user_id = session.get('user_id')
    if not user_id:
        flash('You must be logged in to introduce yourself.', 'danger')
        return redirect(url_for('login'))

    form = {field: request.form.get(field, '').strip() for field in ('location', 'interests', 'about')}
    if not form['about']:
        flash('Please tell everyone a little about yourself before posting your introduction.', 'warning')
        session['intro_form'] = form
        return redirect(url_for('feed'))

    content = f"Hi everyone! I'm new here, my name is {session.get('username')}."
    if form['location']:
        content += f" I'm from {form['location']}."
    if form['interests']:
        content += f" I'm into {form['interests']}."
    content += f" {form['about']}"

    # Introductions go through the same moderation as every other post
    allowed, category, message = moderation_feedback(content, 'introduction')
    if not allowed:
        flash(message + ' Please edit your introduction and try again.', category)
        session['intro_form'] = form
        return redirect(url_for('feed'))

    db = get_db()
    db.execute('INSERT INTO posts (user_id, content, is_introduction) VALUES (?, ?, 1)', (user_id, content))
    db.commit()
    flash('Your introduction is posted! Other members will be invited to welcome you.', 'success')
    if category == 'info':
        flash(message, category)

    added_interests = update_interests_from_posts(user_id)
    if added_interests:
        flash(f'Based on your posts, we added {", ".join(added_interests)} to your interests. '
              'You can edit them on your profile.', 'info')
    return redirect(url_for('feed'))


@app.route('/introduce/skip', methods=['POST'])
def skip_introduction():
    """Hides the Introduction Prompt: "Not now" for this visit, "Don't ask again" for good."""
    user_id = session.get('user_id')
    if not user_id:
        return redirect(url_for('login'))

    if request.form.get('forever'):
        db = get_db()
        db.execute('UPDATE users SET intro_skipped = 1 WHERE id = ?', (user_id,))
        db.commit()
        flash("Okay, we won't ask again. You can still write a normal post to say hello any time.", 'info')
    else:
        session['intro_snoozed'] = True
    return redirect(url_for('feed'))
    
@app.route('/posts/new', methods=['POST'])
def add_post():
    """Handles creating a new post from the feed."""
    user_id = session.get('user_id')

    # Block access if user is not logged in
    if not user_id:
        flash('You must be logged in to create a post.', 'danger')
        return redirect(url_for('login'))

    # Get content from the submitted form
    content = request.form.get('content', '')

    # Basic validation to ensure post is not empty
    if not content.strip():
        flash('Post cannot be empty.', 'warning')
        return redirect(url_for('feed'))

    # Pass the user's content through the moderation function (point-of-action feedback, Design Claim 20)
    allowed, category, message = moderation_feedback(content, 'post')

    # Blocked posts, and flagged posts the user hasn't confirmed yet, are not published.
    # Instead the user is sent back to the post form with a review popup and their draft restored
    if not allowed or (category == 'info' and not request.form.get('confirmed')):
        start_moderation_review(content, 'post', allowed, category, message, url_for('add_post'))
        return redirect(url_for('feed'))

    # Store the original text; moderation is applied again whenever posts are displayed,
    # so the admin risk scores still see the original wording
    db = get_db()
    db.execute('INSERT INTO posts (user_id, content) VALUES (?, ?)',
               (user_id, content))
    db.commit()
    flash(message if category == 'success' else f'Your post was published. {message}', category)

    # Learn the user's interests from what they post
    added_interests = update_interests_from_posts(user_id)
    if added_interests:
        flash(f'Based on your posts, we added {", ".join(added_interests)} to your interests. '
              'You can edit them on your profile.', 'info')

    # Redirect back to the main feed to see the new post
    return redirect(url_for('feed'))


def moderation_issues(content, moderated_content, score):
    """Works out which scored moderation rules a piece of content triggered, as readable descriptions.
    Mirrors the weights in moderate_content: 2.0 per link, 0.5 for excessive caps, 2.0 per mild profanity."""
    issues = []
    remaining = score

    links_removed = moderated_content.count('[LINK REMOVED]') - content.count('[LINK REMOVED]')
    if links_removed > 0:
        issues.append(f'{links_removed} external link(s)')
        remaining -= 2.0 * links_removed

    alphas = [c for c in content if c.isalpha()]
    if len(alphas) > 15 and sum(1 for c in alphas if c.isupper()) / len(alphas) > 0.7:
        issues.append('mostly capital letters')
        remaining -= 0.5

    if remaining > 0:
        issues.append('mild profanity')

    return issues


# Wording that is not against the rules but easily comes across as dismissive to a newcomer
HARSH_PHRASES = [
    'noob', 'n00b', 'newb', 'newbie', 'stupid', 'dumb', 'idiot', 'clueless', 'pathetic', 'useless', 'loser',
    'cringe', 'google it', 'read the rules', 'rtfm', 'lurk more', 'nobody cares', 'who asked', 'shut up',
    'go away', 'learn to', 'did you even', 'what a joke',
]


def find_harsh_words(content):
    """Returns the harsh phrases and mild profanity found in the content, for the newcomer kindness check."""
    found = []
    for phrase in HARSH_PHRASES + TIER3_WORDS:
        match = re.search(r'\b' + re.escape(phrase) + r'\b', content, re.IGNORECASE)
        if match and match.group(0).lower() not in (f.lower() for f in found):
            found.append(match.group(0))
    return found


def moderation_feedback(content, kind):
    """
    Runs content through moderation and decides what the user is told at the moment they submit it.
    kind is 'post' or 'comment'. Returns (allowed, flash_category, message).
    """
    moderated_content, score = moderate_content(content)
    noun = kind.capitalize()

    # Severe violations: Tier 1 words or spam/scam phrases replace the whole text
    if moderated_content.startswith('[Content Removed'):
        if 'Spam/Scam' in moderated_content:
            return False, 'danger', f'{noun} blocked: it contains phrases associated with spam or scams, which violates our spam/scam policy.'
        return False, 'danger', f'{noun} blocked: it contains severe profanity, which violates our community rules.'

    issues = moderation_issues(content, moderated_content, score)

    # Many smaller violations can add up to a severe score too
    if score >= 5.0:
        return False, 'danger', f'{noun} blocked: it has too many rule violations ({", ".join(issues)}). Please revise it and try again.'

    if score > 0:
        return True, 'info', (f'We flagged {", ".join(issues)} in your {kind}. '
                              'Links and profanity are automatically filtered according to community guidelines.')

    return True, 'success', f'Your {kind} was successfully created!'


def start_moderation_review(content, kind, allowed, category, message, action, post_id=None):
    """
    Saves a draft that moderation stopped so the next page can show a review popup.
    The popup lets the user go back to the form with their text restored to edit it,
    or (for flagged but allowed content) publish it anyway by resubmitting to action.
    """
    moderated_content, _ = moderate_content(content)
    session['moderation_review'] = {
        'kind': kind,
        'content': content,
        'allowed': allowed,
        'category': category,
        'message': message,
        # Show how the text will look after filtering, only when it can still be published
        'preview': moderated_content if allowed else None,
        'action': action,
        'post_id': post_id,
    }


@app.context_processor
def inject_moderation_review():
    """
    Hands the review popup and any draft being edited to the templates.
    Both are popped so they are only shown on the next page the user sees.
    """
    return {
        'moderation_review': session.pop('moderation_review', None),
        'moderation_draft': session.pop('moderation_draft', None),
    }


@app.route('/moderation/edit', methods=['POST'])
def moderation_edit():
    """'Edit' button of the review popup: takes the user back to the form with their draft filled in."""
    kind = request.form.get('kind')
    post_id = request.form.get('post_id', type=int)
    session['moderation_draft'] = {
        'kind': kind,
        'content': request.form.get('content', ''),
        'post_id': post_id,
    }

    if kind == 'comment' and post_id:
        return redirect(url_for('post_detail', post_id=post_id) + '#edit-draft')
    return redirect(url_for('feed') + '#new-post-content')
    
    
@app.route('/posts/<int:post_id>/delete', methods=['POST'])
def delete_post(post_id):
    """Handles deleting a post."""
    user_id = session.get('user_id')

    # Block access if user is not logged in
    if not user_id:
        flash('You must be logged in to delete a post.', 'danger')
        return redirect(url_for('login'))

    # Find the post in the database
    post = query_db('SELECT id, user_id FROM posts WHERE id = ?', (post_id,), one=True)

    # Check if the post exists and if the current user is the owner
    if not post:
        flash('Post not found.', 'danger')
        return redirect(url_for('feed'))

    if post['user_id'] != user_id:
        # Security check: prevent users from deleting others' posts
        flash('You do not have permission to delete this post.', 'danger')
        return redirect(url_for('feed'))

    # If all checks pass, proceed with deletion
    db = get_db()
    # To maintain database integrity, delete associated records first
    db.execute('DELETE FROM comments WHERE post_id = ?', (post_id,))
    db.execute('DELETE FROM reactions WHERE post_id = ?', (post_id,))
    # Finally, delete the post itself
    db.execute('DELETE FROM posts WHERE id = ?', (post_id,))
    db.commit()

    flash('Your post was successfully deleted.', 'success')
    # Redirect back to the page the user came from, or the feed as a fallback
    return redirect(request.referrer or url_for('feed'))

@app.route('/u/<username>')
def user_profile(username):
    """Displays a user's profile page with moderated bio, posts, and latest comments."""
    
    user_raw = query_db('SELECT * FROM users WHERE username = ?', (username,), one=True)
    if not user_raw:
        abort(404)

    user = dict(user_raw)
    moderated_bio, _ = moderate_content(user.get('profile', ''))
    user['profile'] = moderated_bio

    posts_raw = query_db('SELECT id, content, user_id, created_at FROM posts WHERE user_id = ? ORDER BY created_at DESC', (user['id'],))
    posts = []
    for post_raw in posts_raw:
        post = dict(post_raw)
        moderated_post_content, _ = moderate_content(post['content'])
        post['content'] = moderated_post_content
        posts.append(post)

    comments_raw = query_db('SELECT id, content, user_id, post_id, created_at FROM comments WHERE user_id = ? ORDER BY created_at DESC LIMIT 100', (user['id'],))
    comments = []
    for comment_raw in comments_raw:
        comment = dict(comment_raw)
        moderated_comment_content, _ = moderate_content(comment['content'])
        comment['content'] = moderated_comment_content
        comments.append(comment)

    followers_count = query_db('SELECT COUNT(*) as cnt FROM follows WHERE followed_id = ?', (user['id'],), one=True)['cnt']
    following_count = query_db('SELECT COUNT(*) as cnt FROM follows WHERE follower_id = ?', (user['id'],), one=True)['cnt']

    #  NEW: CHECK FOLLOW STATUS 
    is_currently_following = False # Default to False
    current_user_id = session.get('user_id')
    
    # We only need to check if a user is logged in
    if current_user_id:
        follow_relation = query_db(
            'SELECT 1 FROM follows WHERE follower_id = ? AND followed_id = ?',
            (current_user_id, user['id']),
            one=True
        )
        if follow_relation:
            is_currently_following = True
    # --
    
    #Retrieve user's interests
    user_interests = [i.strip() for i in (user.get('interests') or '').split(',') if i.strip()]

    shared_interests = []
    current_user_id = session.get('user_id')
    is_own_profile = current_user_id == user['id']

    if current_user_id and not is_own_profile:
        current_user = query_db('SELECT interests FROM users WHERE id = ?', (current_user_id,), one=True)
        if current_user and current_user['interests']:
            current_user_interests = set(i.strip().lower() for i in current_user['interests'].split(',') if i.strip())
            #Calculate Intersection, ignoring case (shown with the profile owner's spelling)
            shared_interests = [i for i in user_interests if i.lower() in current_user_interests]

    # Check if the profile owner is a newcomer (account less than 7 days old)
    account_age_days = get_account_age_days(user['id'])
    is_newcomer = is_newcomer_age(account_age_days)

    return render_template('user_profile.html.j2',
                           user=user, 
                           posts=posts, 
                           comments=comments,
                           followers_count=followers_count, 
                           following_count=following_count,
                           is_following=is_currently_following,
                           shared_interests=shared_interests,
                           user_interests=user_interests,
                           is_own_profile=is_own_profile,
                           account_age_days=account_age_days,
                           is_newcomer=is_newcomer)
    

def split_interests(raw):
    """Turns a stored 'A, B, C' interests string into a list."""
    return [i.strip() for i in (raw or '').split(',') if i.strip()]


def clean_interests(raw):
    """Turns the comma-separated string from the tag input into 'A, B, C', dropping blanks and case-insensitive duplicates."""
    interests_list = []
    for interest in raw.split(','):
        interest = interest.strip()
        if interest and interest.lower() not in (i.lower() for i in interests_list):
            interests_list.append(interest)
    return ', '.join(interests_list)


# Keywords used to recognise each interest in a user's posts
INTEREST_KEYWORDS = {
    'AI': {'ai', 'chatgpt', 'gpt', 'llm', 'llms', 'neural', 'chatbot', 'openai', 'claude', 'gemini'},
    'Python': {'python', 'pip', 'django', 'flask', 'pandas', 'numpy', 'pytest', 'jupyter'},
    'Tech': {'tech', 'technology', 'computer', 'laptop', 'smartphone', 'phone', 'gadget', 'gadgets', 'software',
             'hardware', 'programming', 'coding', 'code', 'developer', 'app', 'linux', 'windows', 'gpu', 'cpu'},
    'Security': {'security', 'cybersecurity', 'hacker', 'hackers', 'hacking', 'malware', 'phishing', 'password',
                 'passwords', 'encryption', 'vulnerability', 'firewall', 'privacy', 'breach'},
    'DIY': {'diy', 'handmade', 'crafts', 'craft', 'woodworking', 'build', 'built', 'building', 'drill', 'paint',
            'painted', 'repair', 'fixed', 'tools', 'renovation', 'workshop'},
    'Cats': {'cat', 'cats', 'kitten', 'kittens', 'kitty', 'meow', 'purr', 'feline'},
    'Dogs': {'dog', 'dogs', 'puppy', 'puppies', 'pup', 'woof', 'bark', 'walkies', 'canine'},
    'Games': {'game', 'games', 'gaming', 'gamer', 'playstation', 'xbox', 'nintendo', 'steam', 'minecraft', 'esports'},
    'Cars': {'car', 'cars', 'bmw', 'ferrari', 'ford', 'audi', 'tesla', 'engine', 'driving', 'motorsport'},
    'Sports': {'sport', 'sports', 'football', 'soccer', 'hockey', 'basketball', 'tennis', 'goal'},
    'Fitness': {'gym', 'workout', 'fitness', 'running', 'exercise', 'training', 'yoga', 'lifting'},
    'Food': {'food', 'cooking', 'cook', 'recipe', 'baking', 'baked', 'dinner', 'lunch', 'pizza', 'pasta', 'delicious'},
    'Music': {'music', 'song', 'songs', 'album', 'concert', 'guitar', 'piano', 'band', 'playlist', 'spotify'},
    'Travel': {'travel', 'travelling', 'traveling', 'trip', 'vacation', 'holiday', 'flight', 'beach', 'hiking', 'abroad'},
    'Movies': {'movie', 'movies', 'film', 'films', 'cinema', 'netflix', 'trailer'},
    'Books': {'book', 'books', 'reading', 'novel', 'author', 'library'},
    'Gardening': {'garden', 'gardening', 'flower', 'flowers', 'plant', 'plants', 'tulip', 'tulips', 'rose',
                  'roses', 'seeds', 'bloom', 'blooming', 'soil', 'greenhouse', 'vegetables', 'houseplants'},
    'Nature': {'nature', 'forest', 'woods', 'lake', 'mountain', 'mountains', 'wildlife', 'birds', 'sunset',
               'outdoors', 'camping', 'river', 'trees'},
    'Photography': {'photo', 'photos', 'photography', 'camera', 'lens', 'photographer', 'shot', 'shots',
                    'portrait', 'canon', 'nikon'},
    'Art': {'art', 'artist', 'drawing', 'sketch', 'painting', 'canvas', 'gallery', 'museum', 'illustration',
            'sculpture', 'watercolor'},
    'Fashion': {'fashion', 'outfit', 'outfits', 'clothes', 'dress', 'shoes', 'sneakers', 'style', 'jacket',
                'streetwear'},
    'Science': {'science', 'scientist', 'physics', 'chemistry', 'biology', 'space', 'nasa', 'planet',
                'research', 'experiment', 'astronomy'},
    'History': {'history', 'historical', 'ancient', 'medieval', 'war', 'empire', 'castle', 'century'},
}

# How many keyword mentions across a user's posts it takes before an interest is added,
# so that a single passing word doesn't count as an interest
INTEREST_MIN_MENTIONS = 2


def detect_interests(texts):
    """Keyword-based interest detection: counts keyword mentions per interest across the texts
    and returns the interests mentioned at least INTEREST_MIN_MENTIONS times, most mentioned first."""
    counts = collections.Counter()
    for text in texts:
        words = re.findall(r"[a-z0-9]+", (text or '').lower())
        for interest, keywords in INTEREST_KEYWORDS.items():
            mentions = sum(1 for word in words if word in keywords)
            if mentions:
                counts[interest] += mentions
    return [interest for interest, n in counts.most_common() if n >= INTEREST_MIN_MENTIONS]


def update_interests_from_posts(user_id):
    """Detects interests from all of the user's posts and adds any new ones to their profile,
    skipping interests the user has removed themselves. Returns the list of interests that were added."""
    posts = query_db('SELECT content FROM posts WHERE user_id = ?', (user_id,))
    detected = detect_interests([p['content'] for p in posts])

    user = query_db('SELECT interests, removed_interests FROM users WHERE id = ?', (user_id,), one=True)
    current = split_interests(user['interests'])
    skip_lower = {i.lower() for i in current + split_interests(user['removed_interests'])}
    added = [i for i in detected if i.lower() not in skip_lower]

    if added:
        db = get_db()
        db.execute('UPDATE users SET interests = ? WHERE id = ?',
                   (clean_interests(', '.join(current + added)), user_id))
        db.commit()
    return added


@app.route('/u/interests', methods=['POST'])
def update_interests():
    """Handles the current user editing their interests from their profile page."""
    user_id = session.get('user_id')

    # Block access if user is not logged in
    if not user_id:
        flash('You must be logged in to edit your interests.', 'danger')
        return redirect(url_for('login'))

    interests = clean_interests(request.form.get('interests', ''))
    new_lower = {i.lower() for i in split_interests(interests)}

    # Remember interests the user took off their profile so post-based detection doesn't add them back.
    # Interests they add again themselves are taken off that list
    user = query_db('SELECT interests, removed_interests FROM users WHERE id = ?', (user_id,), one=True)
    removed = [i for i in split_interests(user['removed_interests']) if i.lower() not in new_lower]
    removed += [i for i in split_interests(user['interests']) if i.lower() not in new_lower]

    db = get_db()
    db.execute('UPDATE users SET interests = ?, removed_interests = ? WHERE id = ?',
               (interests, clean_interests(', '.join(removed)), user_id))
    db.commit()

    flash('Your interests were updated.', 'success')
    return redirect(url_for('user_profile', username=session.get('username')))


@app.route('/u/<username>/followers')
def user_followers(username):
    user = query_db('SELECT * FROM users WHERE username = ?', (username,), one=True)
    if not user:
        abort(404)
    followers = query_db('''
        SELECT u.username
        FROM follows f
        JOIN users u ON f.follower_id = u.id
        WHERE f.followed_id = ?
    ''', (user['id'],))
    return render_template('user_list.html.j2', user=user, users=followers, title="Followers of")

@app.route('/u/<username>/following')
def user_following(username):
    user = query_db('SELECT * FROM users WHERE username = ?', (username,), one=True)
    if not user:
        abort(404)
    following = query_db('''
        SELECT u.username
        FROM follows f
        JOIN users u ON f.followed_id = u.id
        WHERE f.follower_id = ?
    ''', (user['id'],))
    return render_template('user_list.html.j2', user=user, users=following, title="Users followed by")

@app.route('/posts/<int:post_id>')
def post_detail(post_id):
    """Displays a single post and its comments, with content moderation applied."""
    
    post_raw = query_db('''
        SELECT p.id, p.content, p.created_at, u.username, u.id as user_id
        FROM posts p
        JOIN users u ON p.user_id = u.id
        WHERE p.id = ?
    ''', (post_id,), one=True)

    if not post_raw:
        # The abort function will stop the request and show a 404 Not Found page.
        abort(404)

    #  Moderation for the Main Post 
    # Convert the raw database row to a mutable dictionary
    post = dict(post_raw)
    # Unpack the tuple from moderate_content, we only need the moderated content string here
    moderated_post_content, _ = moderate_content(post['content'])
    post['content'] = moderated_post_content

    #  Fetch Reactions (No moderation needed) 
    reactions = query_db('''
        SELECT reaction_type, COUNT(*) as count
        FROM reactions
        WHERE post_id = ?
        GROUP BY reaction_type
    ''', (post_id,))

    #  Fetch and Moderate Comments 
    comments_raw = query_db('SELECT c.id, c.content, c.created_at, u.username, u.id as user_id FROM comments c JOIN users u ON c.user_id = u.id WHERE c.post_id = ? ORDER BY c.created_at ASC', (post_id,))
    
    comments = [] # Create a new list for the moderated comments
    for comment_raw in comments_raw:
        comment = dict(comment_raw) # Convert to a dictionary
        # Moderate the content of each comment
        moderated_comment_content, _ = moderate_content(comment['content'])
        comment['content'] = moderated_comment_content
        comments.append(comment)

    # Check if the post author is a newcomer (account less than 7 days old)
    account_age_days = get_account_age_days(post['user_id'])
    is_newcomer = is_newcomer_age(account_age_days)

    # Pass the moderated data to the template
    return render_template('post_detail.html.j2',
                           post=post,
                           reactions=reactions,
                           comments=comments,
                           reaction_emojis=REACTION_EMOJIS,
                           reaction_types=REACTION_TYPES,
                           is_newcomer=is_newcomer)

@app.route('/about')
def about():
    return render_template('about.html.j2')

@app.route('/privacy')
def privacy():
    return render_template('privacy.html.j2')


@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        location = request.form.get('location', '')
        birthdate = request.form.get('birthdate', '')
        profile = request.form.get('profile', '')

        interests = clean_interests(request.form.get('interests', ''))

        # Same point-of-action moderation feedback as posts and comments
        bio_allowed, bio_category, bio_message = moderation_feedback(profile, 'bio')
        if not bio_allowed:
            flash(bio_message + ' Please edit your bio and try again.', bio_category)
            return render_template('signup.html.j2', form=request.form,
                                   interests=[i.strip() for i in interests.split(',') if i.strip()])

        hashed_password = generate_password_hash(password)

        db = get_db()
        cur = db.cursor()
        try:
            cur.execute(
                'INSERT INTO users (username, password, location, birthdate, profile, interests) VALUES (?, ?, ?, ?, ?, ?)',
                (username, hashed_password, location, birthdate, profile, interests)
            )
            db.commit()

            # 1. Get the ID of the user we just created.
            new_user_id = cur.lastrowid

            # 2. Add user info to the session cookie.
            session.clear() # Clear any old session data
            session['user_id'] = new_user_id
            session['username'] = username

            # 3. Flash a welcome message and redirect to the feed.
            flash(f'Welcome, {username}! Your account has been created.', 'success')
            # Remind the user if their bio was accepted but filtered
            if bio_category == 'info':
                flash(bio_message, bio_category)
            return redirect(url_for('feed')) # Redirect to the main feed/dashboard

        except sqlite3.IntegrityError:
            flash('Username already taken. Please choose another one.', 'danger')
        finally:
            cur.close()
            db.close()

        # Keep what the user typed (except the password) so they don't have to start over
        return render_template('signup.html.j2', form=request.form,
                               interests=[i.strip() for i in interests.split(',') if i.strip()])

    return render_template('signup.html.j2', form={}, interests=[])

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        db = get_db()
        user = db.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        db.close()

        # 1. Check if the user exists.
        # 2. If user exists, use check_password_hash to securely compare the password.
        #    This function handles the salt and prevents timing attacks.
        if user and check_password_hash(user['password'], password):
            # Password is correct!
            session['user_id'] = user['id']
            session['username'] = user['username']
            flash('Logged in successfully.', 'success')
            return redirect(url_for('feed'))
        else:
            # User does not exist or password was incorrect.
            flash('Invalid username or password.', 'danger')
            
    return render_template('login.html.j2')

@app.route('/logout')
def logout():
    session.clear()
    flash('Logged out.', 'info')
    return redirect(url_for('login'))

@app.route('/posts/<int:post_id>/comment', methods=['POST'])
def add_comment(post_id):
    """Handles adding a new comment to a specific post."""
    user_id = session.get('user_id')

    # Block access if user is not logged in
    if not user_id:
        flash('You must be logged in to comment.', 'danger')
        return redirect(url_for('login'))

    # Get content from the submitted form
    content = request.form.get('content')

    # Basic validation to ensure comment is not empty
    if content and content.strip():
        # Same point-of-action moderation feedback as posts: send the user back to the post's
        # comment form with a review popup instead of publishing
        allowed, category, message = moderation_feedback(content, 'comment')
        confirmed = request.form.get('confirmed')
        if not allowed:
            start_moderation_review(content, 'comment', allowed, category, message,
                                    url_for('add_comment', post_id=post_id), post_id)
            return redirect(url_for('post_detail', post_id=post_id))

        post = query_db('SELECT p.user_id, p.is_introduction, u.username FROM posts p '
                        'JOIN users u ON p.user_id = u.id WHERE p.id = ?', (post_id,), one=True)
        author_age_days = get_account_age_days(post['user_id']) if post else None
        to_newcomer = post and post['user_id'] != user_id and is_newcomer_age(author_age_days)

        # Kindness check (Design Claim 21): harsh wording aimed at a newcomer is held back
        # and the commenter is asked to rephrase it before it is published
        harsh_words = find_harsh_words(content) if to_newcomer else []
        if harsh_words and not confirmed:
            joined = {0: 'today', 1: 'yesterday'}.get(author_age_days, f'{author_age_days} days ago')
            quoted = ', '.join(f'"{w}"' for w in harsh_words)
            newcomer_message = (f"{post['username']} joined {joined} and is still finding their way around. "
                                f'Your comment includes {quoted}, which can feel harsh to someone new. '
                                'Could you rephrase it in a friendlier way?')
            start_moderation_review(content, 'comment', True, 'newcomer', newcomer_message,
                                    url_for('add_comment', post_id=post_id), post_id)
            return redirect(url_for('post_detail', post_id=post_id))

        if category == 'info' and not confirmed:
            start_moderation_review(content, 'comment', allowed, category, message,
                                    url_for('add_comment', post_id=post_id), post_id)
            return redirect(url_for('post_detail', post_id=post_id))

        db = get_db()
        db.execute('INSERT INTO comments (post_id, user_id, content) VALUES (?, ?, ?)',
                   (post_id, user_id, content))
        db.commit()
        if post and post['is_introduction'] and post['user_id'] != user_id:
            flash(f"Thanks for welcoming {post['username']}! \U0001F331", 'success')
        else:
            flash(message if category == 'success' else f'Your comment was published. {message}', category)
    else:
        flash('Comment cannot be empty.', 'warning')

    # Redirect back to the page the user came from (likely the post detail page)
    return redirect(request.referrer or url_for('post_detail', post_id=post_id))

@app.route('/comments/<int:comment_id>/delete', methods=['POST'])
def delete_comment(comment_id):
    """Handles deleting a comment."""
    user_id = session.get('user_id')

    # Block access if user is not logged in
    if not user_id:
        flash('You must be logged in to delete a comment.', 'danger')
        return redirect(url_for('login'))

    # Find the comment and the original post's author ID
    comment = query_db('''
        SELECT c.id, c.user_id, p.user_id as post_author_id
        FROM comments c
        JOIN posts p ON c.post_id = p.id
        WHERE c.id = ?
    ''', (comment_id,), one=True)

    # Check if the comment exists
    if not comment:
        flash('Comment not found.', 'danger')
        return redirect(request.referrer or url_for('feed'))

    # Security Check: Allow deletion if the user is the comment's author OR the post's author
    if user_id != comment['user_id'] and user_id != comment['post_author_id']:
        flash('You do not have permission to delete this comment.', 'danger')
        return redirect(request.referrer or url_for('feed'))

    # If all checks pass, proceed with deletion
    db = get_db()
    db.execute('DELETE FROM comments WHERE id = ?', (comment_id,))
    db.commit()

    flash('Comment successfully deleted.', 'success')
    # Redirect back to the page the user came from
    return redirect(request.referrer or url_for('feed'))

@app.route('/react', methods=['POST'])
def add_reaction():
    """Handles adding a new reaction or updating an existing one."""
    user_id = session.get('user_id')

    if not user_id:
        flash("You must be logged in to react.", "danger")
        return redirect(url_for('login'))

    post_id = request.form.get('post_id')
    new_reaction_type = request.form.get('reaction')

    if not post_id or not new_reaction_type:
        flash("Invalid reaction request.", "warning")
        return redirect(request.referrer or url_for('feed'))

    db = get_db()

    # Step 1: Check if a reaction from this user already exists on this post.
    existing_reaction = query_db('SELECT id FROM reactions WHERE post_id = ? AND user_id = ?',
                                 (post_id, user_id), one=True)

    if existing_reaction:
        # Step 2: If it exists, UPDATE the reaction_type.
        db.execute('UPDATE reactions SET reaction_type = ? WHERE id = ?',
                   (new_reaction_type, existing_reaction['id']))
    else:
        # Step 3: If it does not exist, INSERT a new reaction.
        db.execute('INSERT INTO reactions (post_id, user_id, reaction_type) VALUES (?, ?, ?)',
                   (post_id, user_id, new_reaction_type))

    db.commit()

    return redirect(request.referrer or url_for('feed'))

@app.route('/unreact', methods=['POST'])
def unreact():
    """Handles removing a user's reaction from a post."""
    user_id = session.get('user_id')

    if not user_id:
        flash("You must be logged in to unreact.", "danger")
        return redirect(url_for('login'))

    post_id = request.form.get('post_id')

    if not post_id:
        flash("Invalid unreact request.", "warning")
        return redirect(request.referrer or url_for('feed'))

    db = get_db()

    # Remove the reaction if it exists
    existing_reaction = query_db(
        'SELECT id FROM reactions WHERE post_id = ? AND user_id = ?',
        (post_id, user_id),
        one=True
    )

    if existing_reaction:
        db.execute('DELETE FROM reactions WHERE id = ?', (existing_reaction['id'],))
        db.commit()
        flash("Reaction removed.", "success")
    else:
        flash("No reaction to remove.", "info")

    return redirect(request.referrer or url_for('feed'))


@app.route('/u/<int:user_id>/follow', methods=['POST'])
def follow_user(user_id):
    """Handles the logic for the current user to follow another user."""
    follower_id = session.get('user_id')

    # Security: Ensure user is logged in
    if not follower_id:
        flash("You must be logged in to follow users.", "danger")
        return redirect(url_for('login'))

    # Security: Prevent users from following themselves
    if follower_id == user_id:
        flash("You cannot follow yourself.", "warning")
        return redirect(request.referrer or url_for('feed'))

    # Check if the user to be followed actually exists
    user_to_follow = query_db('SELECT id FROM users WHERE id = ?', (user_id,), one=True)
    if not user_to_follow:
        flash("The user you are trying to follow does not exist.", "danger")
        return redirect(request.referrer or url_for('feed'))
        
    db = get_db()
    try:
        # Insert the follow relationship. The PRIMARY KEY constraint will prevent duplicates if you've set one.
        db.execute('INSERT INTO follows (follower_id, followed_id) VALUES (?, ?)',
                   (follower_id, user_id))
        db.commit()
        username_to_follow = query_db('SELECT username FROM users WHERE id = ?', (user_id,), one=True)['username']
        flash(f"You are now following {username_to_follow}.", "success")
    except sqlite3.IntegrityError:
        flash("You are already following this user.", "info")

    return redirect(request.referrer or url_for('feed'))


@app.route('/u/<int:user_id>/unfollow', methods=['POST'])
def unfollow_user(user_id):
    """Handles the logic for the current user to unfollow another user."""
    follower_id = session.get('user_id')

    # Security: Ensure user is logged in
    if not follower_id:
        flash("You must be logged in to unfollow users.", "danger")
        return redirect(url_for('login'))

    db = get_db()
    cur = db.execute('DELETE FROM follows WHERE follower_id = ? AND followed_id = ?',
               (follower_id, user_id))
    db.commit()

    if cur.rowcount > 0:
        # cur.rowcount tells us if a row was actually deleted
        username_unfollowed = query_db('SELECT username FROM users WHERE id = ?', (user_id,), one=True)['username']
        flash(f"You have unfollowed {username_unfollowed}.", "success")
    else:
        # This case handles if someone tries to unfollow a user they weren't following
        flash("You were not following this user.", "info")

    # Redirect back to the page the user came from
    return redirect(request.referrer or url_for('feed'))

@app.route('/admin')
def admin_dashboard():
    """Displays the admin dashboard with users, posts, and comments, sorted by risk."""

    if session.get('username') != 'admin':
        flash("You do not have permission to access this page.", "danger")
        return redirect(url_for('feed'))

    RISK_LEVELS = { "HIGH": 5, "MEDIUM": 3, "LOW": 1 }
    PAGE_SIZE = 50

    def get_risk_profile(score):
        if score >= RISK_LEVELS["HIGH"]:
            return "HIGH", 3
        elif score >= RISK_LEVELS["MEDIUM"]:
            return "MEDIUM", 2
        elif score >= RISK_LEVELS["LOW"]:
            return "LOW", 1
        return "NONE", 0

    # Get pagination and current tab parameters
    try:
        users_page = int(request.args.get('users_page', 1))
        posts_page = int(request.args.get('posts_page', 1))
        comments_page = int(request.args.get('comments_page', 1))
    except ValueError:
        users_page = 1
        posts_page = 1
        comments_page = 1
    
    current_tab = request.args.get('tab', 'users') # Default to 'users' tab

    users_offset = (users_page - 1) * PAGE_SIZE
    
    # First, get all users to calculate risk, then apply pagination in Python
    # It's more complex to do this efficiently in SQL if risk calc is Python-side
    all_users_raw = query_db('SELECT id, username, profile, created_at FROM users')
    all_users = []
    for user in all_users_raw:
        user_dict = dict(user)
        user_risk_score = user_risk_analysis(user_dict['id'])
        risk_label, risk_sort_key = get_risk_profile(user_risk_score)
        user_dict['risk_label'] = risk_label
        user_dict['risk_sort_key'] = risk_sort_key
        user_dict['risk_score'] = min(5.0, round(user_risk_score, 2))
        all_users.append(user_dict)

    all_users.sort(key=lambda x: x['risk_score'], reverse=True)
    total_users = len(all_users)
    users = all_users[users_offset : users_offset + PAGE_SIZE]
    total_users_pages = (total_users + PAGE_SIZE - 1) // PAGE_SIZE

    # --- Posts Tab Data ---
    posts_offset = (posts_page - 1) * PAGE_SIZE
    total_posts_count = query_db('SELECT COUNT(*) as count FROM posts', one=True)['count']
    total_posts_pages = (total_posts_count + PAGE_SIZE - 1) // PAGE_SIZE

    posts_raw = query_db(f'''
        SELECT p.id, p.content, p.created_at, u.username, u.created_at as user_created_at
        FROM posts p JOIN users u ON p.user_id = u.id
        ORDER BY p.id DESC -- Order by ID for consistent pagination before risk sort
        LIMIT ? OFFSET ?
    ''', (PAGE_SIZE, posts_offset))
    posts = []
    for post in posts_raw:
        post_dict = dict(post)
        _, base_score = moderate_content(post_dict['content'])
        final_score = base_score 
        author_created_dt = post_dict['user_created_at']
        author_age_days = (datetime.utcnow() - author_created_dt).days
        if author_age_days < 7:
            final_score *= 1.5
        risk_label, risk_sort_key = get_risk_profile(final_score)
        post_dict['risk_label'] = risk_label
        post_dict['risk_sort_key'] = risk_sort_key
        post_dict['risk_score'] = round(final_score, 2)
        posts.append(post_dict)

    posts.sort(key=lambda x: x['risk_score'], reverse=True) # Sort after fetching and scoring

    # --- Comments Tab Data ---
    comments_offset = (comments_page - 1) * PAGE_SIZE
    total_comments_count = query_db('SELECT COUNT(*) as count FROM comments', one=True)['count']
    total_comments_pages = (total_comments_count + PAGE_SIZE - 1) // PAGE_SIZE

    comments_raw = query_db(f'''
        SELECT c.id, c.content, c.created_at, u.username, u.created_at as user_created_at
        FROM comments c JOIN users u ON c.user_id = u.id
        ORDER BY c.id DESC -- Order by ID for consistent pagination before risk sort
        LIMIT ? OFFSET ?
    ''', (PAGE_SIZE, comments_offset))
    comments = []
    for comment in comments_raw:
        comment_dict = dict(comment)
        _, score = moderate_content(comment_dict['content'])
        author_created_dt = comment_dict['user_created_at']
        author_age_days = (datetime.utcnow() - author_created_dt).days
        if author_age_days < 7:
            score *= 1.5
        risk_label, risk_sort_key = get_risk_profile(score)
        comment_dict['risk_label'] = risk_label
        comment_dict['risk_sort_key'] = risk_sort_key
        comment_dict['risk_score'] = round(score, 2)
        comments.append(comment_dict)

    comments.sort(key=lambda x: x['risk_score'], reverse=True) # Sort after fetching and scoring


    return render_template('admin.html.j2', 
                           users=users, 
                           posts=posts, 
                           comments=comments,
                           
                           # Pagination for Users
                           users_page=users_page,
                           total_users_pages=total_users_pages,
                           users_has_next=(users_page < total_users_pages),
                           users_has_prev=(users_page > 1),

                           # Pagination for Posts
                           posts_page=posts_page,
                           total_posts_pages=total_posts_pages,
                           posts_has_next=(posts_page < total_posts_pages),
                           posts_has_prev=(posts_page > 1),

                           # Pagination for Comments
                           comments_page=comments_page,
                           total_comments_pages=total_comments_pages,
                           comments_has_next=(comments_page < total_comments_pages),
                           comments_has_prev=(comments_page > 1),

                           current_tab=current_tab,
                           PAGE_SIZE=PAGE_SIZE)



@app.route('/admin/delete/user/<int:user_id>', methods=['POST'])
def admin_delete_user(user_id):
    if session.get('username') != 'admin':
        flash("You do not have permission to perform this action.", "danger")
        return redirect(url_for('feed'))
        
    if user_id == session.get('user_id'):
        flash('You cannot delete your own account from the admin panel.', 'danger')
        return redirect(url_for('admin_dashboard'))
    
    db = get_db()
    db.execute('DELETE FROM users WHERE id = ?', (user_id,))
    db.commit()
    flash(f'User {user_id} and all their content has been deleted.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/delete/post/<int:post_id>', methods=['POST'])
def admin_delete_post(post_id):
    if session.get('username') != 'admin':
        flash("You do not have permission to perform this action.", "danger")
        return redirect(url_for('feed'))

    db = get_db()
    db.execute('DELETE FROM comments WHERE post_id = ?', (post_id,))
    db.execute('DELETE FROM reactions WHERE post_id = ?', (post_id,))
    db.execute('DELETE FROM posts WHERE id = ?', (post_id,))
    db.commit()
    flash(f'Post {post_id} has been deleted.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/delete/comment/<int:comment_id>', methods=['POST'])
def admin_delete_comment(comment_id):
    if session.get('username') != 'admin':
        flash("You do not have permission to perform this action.", "danger")
        return redirect(url_for('feed'))

    db = get_db()
    db.execute('DELETE FROM comments WHERE id = ?', (comment_id,))
    db.commit()
    flash(f'Comment {comment_id} has been deleted.', 'success')
    return redirect(url_for('admin_dashboard'))

@app.route('/rules')
def rules():
    return render_template('rules.html.j2')

@app.template_global()
def loop_color(user_id):
    # Generate a pastel color based on user_id hash
    h = hashlib.md5(str(user_id).encode()).hexdigest()
    r = int(h[0:2], 16)
    g = int(h[2:4], 16)
    b = int(h[4:6], 16)
    return f'rgb({r % 128 + 80}, {g % 128 + 80}, {b % 128 + 80})'


# ----- Functions to be implemented are below
# Coding Assignment #2

# Assignment 2.2
def user_risk_analysis(user_id):
    
    user = query_db('SELECT profile, created_at FROM users WHERE id = ?', (user_id,), one=True)
    
    if not user:
        return 0.0
    
    #Step 1: Profile Score
    profile_text = user['profile'] or ""
    _, profile_score = moderate_content(profile_text)
    
    #Step 2: Average Post Score
    user_posts = query_db('SELECT content FROM posts WHERE user_id = ?', (user_id,))
    if user_posts:
        post_scores = [moderate_content(p['content'])[1] for p in user_posts]
        avg_post_score = sum(post_scores) / len(post_scores)
    else:
        avg_post_score = 0.0
        
    #Step 3: Average Comment Score
    user_comments = query_db('SELECT content FROM comments WHERE user_id = ?', (user_id,))
    if user_comments:
        comment_scores = [moderate_content(c['content'])[1] for c in user_comments]
        avg_comment_score = sum(comment_scores) / len(comment_scores)
    else:
        avg_comment_score = 0.0

    #Step 4: Combine Scores with Weighting
    content_risk_score = (profile_score * 1.0) + (avg_post_score * 3.0) + (avg_comment_score * 1.0)
    
    #Step 5: Apply Account Age Multiplier
    created_at = user['created_at']
    if isinstance(created_at, str):
        created_at = datetime.strptime(created_at, '%Y-%m-%d %H:%M:%S')
        
    # SQLite's CURRENT_TIMESTAMP is in UTC, so compare against UTC
    account_age_days = (datetime.utcnow() - created_at).days

    if account_age_days < 7:
        multiplier = 1.5
    elif account_age_days < 30:
        multiplier = 1.2
    else:
        multiplier = 1.0
        
    user_risk_score = content_risk_score * multiplier
    
    #Step 6: Cap the score at 5.0
    return min(5.0, user_risk_score)


    
# Assignment 2.1
def moderate_content(content):
    
    if not content:
        return "", 0.0

    moderated_content = content
    score = 0
    
    #Level 1: Severe Violations
    #Rule 1.1.1: Tier 1 words
    
    for word in TIER1_WORDS:
        pattern = r'\b' + re.escape(word) + r'\b'
        if re.search(pattern, moderated_content, re.IGNORECASE):
            return "[Content Removed Due to Severe Violation]", 5.0
        
    #Rule 1.1.2: Tier 2 words
    for word in TIER2_PHRASES:
        if re.search(re.escape(word), moderated_content, re.IGNORECASE):
            return "[Content Removed Due to Spam/Scam Policy]", 5.0
        
    #Level 2: Scored Violations and Filtering
    #Rule 1.2.1: Tier 3 words
    for word in TIER3_WORDS:
        pattern = r'\b' + re.escape(word) + r'\b'
        matches = re.findall(pattern, moderated_content, re.IGNORECASE)
        if matches:
            score += 2.0 * len(matches)
            moderated_content = re.sub(pattern, lambda m: '*' * len(m.group(0)), moderated_content, flags=re.IGNORECASE)
            
    #Rule 1.2.2: External Links
    external_link_pattern = r'https?://\S+|www\.\S+'
    urls = re.findall(external_link_pattern, moderated_content)
    if urls:
        score += 2.0 * len(urls)
        moderated_content = re.sub(external_link_pattern, '[LINK REMOVED]', moderated_content)
        
    #Rule 1.2.3: Excessive Capitalization
    alphas = [c for c in content if c.isalpha()]
    if len(alphas) > 15:
        uppercase_count = sum(1 for c in alphas if c.isupper())
        if uppercase_count / len(alphas) > 0.7:
            score += 0.5
    
    return moderated_content, score

# Coding Assignment #3
# Assignment 3.1
POSITIVE_REACTIONS = ('like', 'love', 'laugh', 'wow')

# Common words that say nothing about a post's topic
STOPWORDS = set('''
the and for are but not you your yours all any can had has have her his him its our out was were will with this that
these those them they their there then than what when where which while who whom why how just about into from over
under again more most some such only own same very too off once here both each few other also get got been being
did does doing would could should may might must shall one two today day time really much new now like love so my me
'''.split())


def tokenize(text):
    """Splits text into lowercase topic words, dropping hashtag symbols, short words and stopwords."""
    return [w for w in re.findall(r'[a-z]+', (text or '').lower()) if len(w) >= 3 and w not in STOPWORDS]


def recommend(user_id, filter_following):
    """
    Recommends 5 relevant posts based on the posts the user reacted to positively and the users they follow.

    Each candidate post (not written by the user, not already reacted to) gets a relevance score:
      +0-10 topic similarity: TF-IDF cosine similarity between the post and the user's liked posts.
            TF (term frequency): how often a word appears in the post, and in how many liked posts for the
            user profile, so words that run through many liked posts define the user's interests.
            IDF (inverse document frequency): rare topic words like "diy" count far more than words that
            appear in many posts.
      +2.0  if its author is someone the user follows
      +1.0  if its author wrote a post the user reacted to positively
    Topic similarity carries the most weight so recommendations stay on-topic; the author bonuses
    move posts from followed and liked authors up without letting their off-topic posts take over.
    The 5 highest-scoring posts are returned in reverse-chronological order.
    """
    base_query = '''SELECT p.id, p.content, p.created_at, u.username, u.id as user_id
                    FROM posts p JOIN users u ON p.user_id = u.id'''

    # Logged-out users have no history, so show the newest posts
    if not user_id:
        return query_db(base_query + ' ORDER BY p.created_at DESC LIMIT 5')

    # 1. Users the current user follows
    followed_ids = {r['followed_id'] for r in query_db('SELECT followed_id FROM follows WHERE follower_id = ?', (user_id,))}

    # 2. Posts the current user reacted to positively, and their authors
    placeholders = ','.join('?' * len(POSITIVE_REACTIONS))
    liked_posts = query_db(f'''SELECT p.id, p.content, p.user_id FROM reactions r JOIN posts p ON r.post_id = p.id
                               WHERE r.user_id = ? AND r.reaction_type IN ({placeholders})''',
                           (user_id, *POSITIVE_REACTIONS))
    liked_author_ids = {p['user_id'] for p in liked_posts}

    # 3. Candidate posts: not the user's own, and not already reacted to
    candidates = query_db(base_query + ''' WHERE p.user_id != ?
                          AND p.id NOT IN (SELECT post_id FROM reactions WHERE user_id = ?)''', (user_id, user_id))
    if filter_following:
        candidates = [p for p in candidates if p['user_id'] in followed_ids]

    # Cold start: nothing to base recommendations on yet, so show the newest posts
    if not followed_ids and not liked_posts:
        return sorted(candidates, key=lambda p: p['created_at'], reverse=True)[:5]

    # 4. IDF: weight each word by how rare it is across all posts
    all_posts = query_db('SELECT content FROM posts')
    doc_freq = collections.Counter()
    for p in all_posts:
        doc_freq.update(set(tokenize(p['content'])))
    total_posts = len(all_posts)
    idf = lambda word: math.log(total_posts / (1 + doc_freq[word]))

    def tfidf_vector(term_counts):
        """Turns {word: TF} into {word: TF * IDF} plus the vector's length, for cosine similarity."""
        vector = {w: tf * idf(w) for w, tf in term_counts.items()}
        return vector, math.sqrt(sum(v * v for v in vector.values()))

    # User profile: TF = number of liked posts each word appears in
    profile_counts = collections.Counter()
    for p in liked_posts:
        profile_counts.update(set(tokenize(p['content'])))
    profile_vector, profile_norm = tfidf_vector(profile_counts)

    # 5. Score each candidate
    scored_posts = []
    for post in candidates:
        score = 0.0
        if post['user_id'] in followed_ids:
            score += 2.0
        if post['user_id'] in liked_author_ids:
            score += 1.0
        # Topic similarity: cosine similarity of TF-IDF vectors (0-1), scaled to 0-10
        # Post vector: TF = number of times each word appears in the post
        post_vector, post_norm = tfidf_vector(collections.Counter(tokenize(post['content'])))
        if post_norm > 0 and profile_norm > 0:
            dot = sum(weight * profile_vector.get(w, 0.0) for w, weight in post_vector.items())
            score += 10.0 * dot / (post_norm * profile_norm)
        if score > 0:
            scored_posts.append((score, post))

    # 6. Take the 5 most relevant (newest first on ties), then show them in reverse-chronological order.
    # Copies of a post the user already liked, or of a post already picked, are skipped: reposted
    # text would otherwise match the profile almost perfectly and fill the whole list
    scored_posts.sort(key=lambda x: (x[0], x[1]['created_at']), reverse=True)
    seen_texts = {tuple(tokenize(p['content'])) for p in liked_posts}
    top_posts = []
    for score, post in scored_posts:
        text = tuple(tokenize(post['content']))
        if text in seen_texts:
            continue
        seen_texts.add(text)
        top_posts.append(post)
        if len(top_posts) == 5:
            break
    return sorted(top_posts, key=lambda p: p['created_at'], reverse=True)


if __name__ == '__main__':
    app.run(debug=True, port=8080)

