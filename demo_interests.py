"""
Demonstration: a new user's interests are learned from their posts.

Signs up a brand-new user with no interests, makes a few posts through the app,
and prints the user's interests after each post. Runs on a temporary copy of
the database, so database.sqlite is not changed.

Usage: python demo_interests.py
"""
import os
import shutil
import sqlite3
import tempfile

import app as social_app

DEMO_POSTS = [
    'Good morning everyone!',
    'Just adopted a kitten, she will not stop purring',
    'Spent the weekend on a woodworking build, made a shelf for my cat',
    'Watched a great movie last night, best film I have seen at the cinema this year',
]


def main():
    # Work on a throwaway copy of the database
    demo_db = os.path.join(tempfile.mkdtemp(), 'demo.sqlite')
    shutil.copy(social_app.DATABASE, demo_db)
    social_app.DATABASE = demo_db

    client = social_app.app.test_client()
    client.post('/signup', data={'username': 'demo_newcomer', 'password': 'demo', 'interests': ''})

    def interests():
        row = sqlite3.connect(demo_db).execute(
            'SELECT interests FROM users WHERE username = ?', ('demo_newcomer',)).fetchone()
        return row[0] or '(none)'

    print(f'New user "demo_newcomer" signed up. Interests: {interests()}')
    print(f'An interest is added once its keywords appear {social_app.INTEREST_MIN_MENTIONS}+ times across the user\'s posts.\n')

    for i, post in enumerate(DEMO_POSTS, start=1):
        client.post('/posts/new', data={'content': post})
        print(f'Post {i}: "{post}"')
        print(f'  -> Interests: {interests()}\n')

    # The user removes an interest on their profile; posting about it again must not bring it back
    kept = ', '.join(i for i in interests().split(', ') if i != 'Cats')
    client.post('/u/interests', data={'interests': kept})
    print(f'User removes "Cats" on their profile. Interests: {interests()}\n')

    post = 'My cat knocked my coffee over again, cats are chaos'
    client.post('/posts/new', data={'content': post})
    print(f'Post {len(DEMO_POSTS) + 1}: "{post}"')
    print(f'  -> Interests: {interests()}  (Cats stays removed)')


if __name__ == '__main__':
    main()
