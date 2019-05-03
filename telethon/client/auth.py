import datetime
import getpass
import hashlib
import inspect
import os
import sys

from .messageparse import MessageParseMethods
from .users import UserMethods
from .. import utils, helpers, errors, password as pwd_mod
from ..tl import types, functions


class AuthMethods(MessageParseMethods, UserMethods):

    # region Public methods

    def start(
            self,
            phone=lambda: input('Please enter your phone (or bot token): '),
            password=lambda: getpass.getpass('Please enter your password: '),
            *,
            bot_token=None, force_sms=False, code_callback=None,
            first_name='New User', last_name='', max_attempts=3):
        """
        Convenience method to interactively connect and sign in if required,
        also taking into consideration that 2FA may be enabled in the account.

        If the phone doesn't belong to an existing account (and will hence
        `sign_up` for a new one),  **you are agreeing to Telegram's
        Terms of Service. This is required and your account
        will be banned otherwise.** See https://telegram.org/tos
        and https://core.telegram.org/api/terms.

        Example usage:
            >>> client = ...
            >>> client.start(phone)
            Please enter the code you received: 12345
            Please enter your password: *******
            (You are now logged in)

        If the event loop is already running, this method returns a
        coroutine that you should await on your own code; otherwise
        the loop is ran until said coroutine completes.

        Args:
            phone (`str` | `int` | `callable`):
                The phone (or callable without arguments to get it)
                to which the code will be sent. If a bot-token-like
                string is given, it will be used as such instead.
                The argument may be a coroutine.

            password (`str`, `callable`, optional):
                The password for 2 Factor Authentication (2FA).
                This is only required if it is enabled in your account.
                The argument may be a coroutine.

            bot_token (`str`):
                Bot Token obtained by `@BotFather <https://t.me/BotFather>`_
                to log in as a bot. Cannot be specified with ``phone`` (only
                one of either allowed).

            force_sms (`bool`, optional):
                Whether to force sending the code request as SMS.
                This only makes sense when signing in with a `phone`.

            code_callback (`callable`, optional):
                A callable that will be used to retrieve the Telegram
                login code. Defaults to `input()`.
                The argument may be a coroutine.

            first_name (`str`, optional):
                The first name to be used if signing up. This has no
                effect if the account already exists and you sign in.

            last_name (`str`, optional):
                Similar to the first name, but for the last. Optional.

            max_attempts (`int`, optional):
                How many times the code/password callback should be
                retried or switching between signing in and signing up.

        Returns:
            This `TelegramClient`, so initialization
            can be chained with ``.start()``.
        """
        if code_callback is None:
            def code_callback():
                return input('Please enter the code you received: ')
        elif not callable(code_callback):
            raise ValueError(
                'The code_callback parameter needs to be a callable '
                'function that returns the code you received by Telegram.'
            )

        if not phone and not bot_token:
            raise ValueError('No phone number or bot token provided.')

        if phone and bot_token and not callable(phone):
            raise ValueError('Both a phone and a bot token provided, '
                             'must only provide one of either')

        coro = self._start(
            phone=phone,
            password=password,
            bot_token=bot_token,
            force_sms=force_sms,
            code_callback=code_callback,
            first_name=first_name,
            last_name=last_name,
            max_attempts=max_attempts
        )
        return (
            coro if self.loop.is_running()
            else self.loop.run_until_complete(coro)
        )

    async def _start(
            self, phone, password, bot_token, force_sms,
            code_callback, first_name, last_name, max_attempts):
        if not self.is_connected():
            await self.connect()

        if await self.is_user_authorized():
            return self

        if not bot_token:
            # Turn the callable into a valid phone number (or bot token)
            while callable(phone):
                value = phone()
                if inspect.isawaitable(value):
                    value = await value

                if ':' in value:
                    # Bot tokens have 'user_id:access_hash' format
                    bot_token = value
                    break

                phone = utils.parse_phone(value) or phone

        if bot_token:
            await self.sign_in(bot_token=bot_token)
            return self

        me = None
        attempts = 0
        two_step_detected = False

        sent_code = await self.send_code_request(phone, force_sms=force_sms)
        sign_up = not sent_code.phone_registered
        while attempts < max_attempts:
            try:
                value = code_callback()
                if inspect.isawaitable(value):
                    value = await value

                # Since sign-in with no code works (it sends the code)
                # we must double-check that here. Else we'll assume we
                # logged in, and it will return None as the User.
                if not value:
                    raise errors.PhoneCodeEmptyError(request=None)

                if sign_up:
                    me = await self.sign_up(value, first_name, last_name)
                else:
                    # Raises SessionPasswordNeededError if 2FA enabled
                    me = await self.sign_in(phone, code=value)
                break
            except errors.SessionPasswordNeededError:
                two_step_detected = True
                break
            except errors.PhoneNumberOccupiedError:
                sign_up = False
            except errors.PhoneNumberUnoccupiedError:
                sign_up = True
            except (errors.PhoneCodeEmptyError,
                    errors.PhoneCodeExpiredError,
                    errors.PhoneCodeHashEmptyError,
                    errors.PhoneCodeInvalidError):
                print('Invalid code. Please try again.', file=sys.stderr)

            attempts += 1
        else:
            raise RuntimeError(
                '{} consecutive sign-in attempts failed. Aborting'
                .format(max_attempts)
            )

        if two_step_detected:
            if not password:
                raise ValueError(
                    "Two-step verification is enabled for this account. "
                    "Please provide the 'password' argument to 'start()'."
                )

            if callable(password):
                for _ in range(max_attempts):
                    try:
                        value = password()
                        if inspect.isawaitable(value):
                            value = await value

                        me = await self.sign_in(phone=phone, password=value)
                        break
                    except errors.PasswordHashInvalidError:
                        print('Invalid password. Please try again',
                              file=sys.stderr)
                else:
                    raise errors.PasswordHashInvalidError(None)
            else:
                me = await self.sign_in(phone=phone, password=password)

        # We won't reach here if any step failed (exit by exception)
        signed, name = 'Signed in successfully as', utils.get_display_name(me)
        try:
            print(signed, name)
        except UnicodeEncodeError:
            # Some terminals don't support certain characters
            print(signed, name.encode('utf-8', errors='ignore')
                              .decode('ascii', errors='ignore'))

        return self

    def _parse_phone_and_hash(self, phone, phone_hash):
        """
        Helper method to both parse and validate phone and its hash.
        """
        phone = utils.parse_phone(phone) or self._phone
        if not phone:
            raise ValueError(
                'Please make sure to call send_code_request first.'
            )

        phone_hash = phone_hash or self._phone_code_hash.get(phone, None)
        if not phone_hash:
            raise ValueError('You also need to provide a phone_code_hash.')

        return phone, phone_hash

    async def sign_in(
            self, phone=None, code=None, *, password=None,
            bot_token=None, phone_code_hash=None):
        """
        Starts or completes the sign in process with the given phone number
        or code that Telegram sent.

        Args:
            phone (`str` | `int`):
                The phone to send the code to if no code was provided,
                or to override the phone that was previously used with
                these requests.

            code (`str` | `int`):
                The code that Telegram sent. Note that if you have sent this
                code through the application itself it will immediately
                expire. If you want to send the code, obfuscate it somehow.
                If you're not doing any of this you can ignore this note.

            password (`str`):
                2FA password, should be used if a previous call raised
                SessionPasswordNeededError.

            bot_token (`str`):
                Used to sign in as a bot. Not all requests will be available.
                This should be the hash the @BotFather gave you.

            phone_code_hash (`str`, optional):
                The hash returned by `send_code_request`. This can be left as
                ``None`` to use the last hash known for the phone to be used.

        Returns:
            The signed in user, or the information about
            :meth:`send_code_request`.
        """
        me = await self.get_me()
        if me:
            return me

        if phone and not code and not password:
            return await self.send_code_request(phone)
        elif code:
            phone, phone_code_hash = \
                self._parse_phone_and_hash(phone, phone_code_hash)

            # May raise PhoneCodeEmptyError, PhoneCodeExpiredError,
            # PhoneCodeHashEmptyError or PhoneCodeInvalidError.
            result = await self(functions.auth.SignInRequest(
                phone, phone_code_hash, str(code)))
        elif password:
            pwd = await self(functions.account.GetPasswordRequest())
            result = await self(functions.auth.CheckPasswordRequest(
                pwd_mod.compute_check(pwd, password)
            ))
        elif bot_token:
            result = await self(functions.auth.ImportBotAuthorizationRequest(
                flags=0, bot_auth_token=bot_token,
                api_id=self.api_id, api_hash=self.api_hash
            ))
        else:
            raise ValueError(
                'You must provide a phone and a code the first time, '
                'and a password only if an RPCError was raised before.'
            )

        return self._on_login(result.user)

    async def sign_up(self, code, first_name, last_name='',
                      *, phone=None, phone_code_hash=None):
        """
        Signs up to Telegram if you don't have an account yet.
        You must call .send_code_request(phone) first.

        **By using this method you're agreeing to Telegram's
        Terms of Service. This is required and your account
        will be banned otherwise.** See https://telegram.org/tos
        and https://core.telegram.org/api/terms.

        Args:
            code (`str` | `int`):
                The code sent by Telegram

            first_name (`str`):
                The first name to be used by the new account.

            last_name (`str`, optional)
                Optional last name.

            phone (`str` | `int`, optional):
                The phone to sign up. This will be the last phone used by
                default (you normally don't need to set this).

            phone_code_hash (`str`, optional):
                The hash returned by `send_code_request`. This can be left as
                ``None`` to use the last hash known for the phone to be used.

        Returns:
            The new created :tl:`User`.
        """
        me = await self.get_me()
        if me:
            return me

        if self._tos and self._tos.text:
            if self.parse_mode:
                t = self.parse_mode.unparse(self._tos.text, self._tos.entities)
            else:
                t = self._tos.text
            sys.stderr.write("{}\n".format(t))
            sys.stderr.flush()

        phone, phone_code_hash = \
            self._parse_phone_and_hash(phone, phone_code_hash)

        result = await self(functions.auth.SignUpRequest(
            phone_number=phone,
            phone_code_hash=phone_code_hash,
            phone_code=str(code),
            first_name=first_name,
            last_name=last_name
        ))

        if self._tos:
            await self(
                functions.help.AcceptTermsOfServiceRequest(self._tos.id))

        return self._on_login(result.user)

    def _on_login(self, user):
        """
        Callback called whenever the login or sign up process completes.

        Returns the input user parameter.
        """
        self._bot = bool(user.bot)
        self._self_input_peer = utils.get_input_peer(user, allow_self=False)
        self._authorized = True

        return user

    async def send_code_request(self, phone, *, force_sms=False):
        """
        Sends a code request to the specified phone number.

        Args:
            phone (`str` | `int`):
                The phone to which the code will be sent.

            force_sms (`bool`, optional):
                Whether to force sending as SMS.

        Returns:
            An instance of :tl:`SentCode`.
        """
        result = None
        phone = utils.parse_phone(phone) or self._phone
        phone_hash = self._phone_code_hash.get(phone)

        if not phone_hash:
            try:
                result = await self(functions.auth.SendCodeRequest(
                    phone, self.api_id, self.api_hash, types.CodeSettings()))
            except errors.AuthRestartError:
                return await self.send_code_request(phone, force_sms=force_sms)

            self._tos = result.terms_of_service
            self._phone_code_hash[phone] = phone_hash = result.phone_code_hash
        else:
            force_sms = True

        self._phone = phone

        if force_sms:
            result = await self(
                functions.auth.ResendCodeRequest(phone, phone_hash))

            self._phone_code_hash[phone] = result.phone_code_hash

        return result

    async def log_out(self):
        """
        Logs out Telegram and deletes the current ``*.session`` file.

        Returns:
            ``True`` if the operation was successful.
        """
        try:
            await self(functions.auth.LogOutRequest())
        except errors.RPCError:
            return False

        self._bot = None
        self._self_input_peer = None
        self._authorized = False
        self._state_cache.reset()

        await self.disconnect()
        self.session.delete()
        return True

    async def edit_2fa(
            self, current_password=None, new_password=None,
            *, hint='', email=None, email_code_callback=None):
        """
        Changes the 2FA settings of the logged in user, according to the
        passed parameters. Take note of the parameter explanations.

        Note that this method may be *incredibly* slow depending on the
        prime numbers that must be used during the process to make sure
        that everything is safe.

        Has no effect if both current and new password are omitted.

        current_password (`str`, optional):
            The current password, to authorize changing to ``new_password``.
            Must be set if changing existing 2FA settings.
            Must **not** be set if 2FA is currently disabled.
            Passing this by itself will remove 2FA (if correct).

        new_password (`str`, optional):
            The password to set as 2FA.
            If 2FA was already enabled, ``current_password`` **must** be set.
            Leaving this blank or ``None`` will remove the password.

        hint (`str`, optional):
            Hint to be displayed by Telegram when it asks for 2FA.
            Leaving unspecified is highly discouraged.
            Has no effect if ``new_password`` is not set.

        email (`str`, optional):
            Recovery and verification email. If present, you must also
            set `email_code_callback`, else it raises ``ValueError``.

        email_code_callback (`callable`, optional):
            If an email is provided, a callback that returns the code sent
            to it must also be set. This callback may be asynchronous.
            It should return a string with the code. The length of the
            code will be passed to the callback as an input parameter.

            If the callback returns an invalid code, it will raise
            ``CodeInvalidError``.

        Returns:
            ``True`` if successful, ``False`` otherwise.
        """
        if new_password is None and current_password is None:
            return False

        if email and not callable(email_code_callback):
            raise ValueError('email present without email_code_callback')

        pwd = await self(functions.account.GetPasswordRequest())
        pwd.new_algo.salt1 += os.urandom(32)
        assert isinstance(pwd, types.account.Password)
        if not pwd.has_password and current_password:
            current_password = None

        if current_password:
            password = pwd_mod.compute_check(pwd, current_password)
        else:
            password = types.InputCheckPasswordEmpty()

        if new_password:
            new_password_hash = pwd_mod.compute_digest(
                pwd.new_algo, new_password)
        else:
            new_password_hash = b''

        try:
            await self(functions.account.UpdatePasswordSettingsRequest(
                password=password,
                new_settings=types.account.PasswordInputSettings(
                    new_algo=pwd.new_algo,
                    new_password_hash=new_password_hash,
                    hint=hint,
                    email=email,
                    new_secure_settings=None
                )
            ))
        except errors.EmailUnconfirmedError as e:
            code = email_code_callback(e.code_length)
            if inspect.isawaitable(code):
                code = await code

            code = str(code)
            await self(functions.account.ConfirmPasswordEmailRequest(code))

        return True

    # endregion

    # region with blocks

    async def __aenter__(self):
        return await self.start()

    async def __aexit__(self, *args):
        await self.disconnect()

    __enter__ = helpers._sync_enter
    __exit__ = helpers._sync_exit

    # endregion
