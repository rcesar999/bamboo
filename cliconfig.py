#!/usr/bin/python
# Automatic deployment and config of Palo on Dock

import argparse, logging, re, time, paramiko, socket

class EndaceProbeCLISession():
    """
    Create new SSH Object

    Placeholder variables that can be replaced as script is read from file are:
        - VMNAME : this is the name of the VM on the EndaceProbe
        - URL : path to location of image volume
        - VOLUME : name of the VM volume on the EndaceProbe
    """

    cpu_virt_list = []


    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self.logger.setLevel(logging.INFO)
        self.out = logging.StreamHandler()
        self.logger.addHandler(self.out)
        self.vars = { 'URL': None, 'VMNAME': 'default-vm', 'IMAGE': None }
        self.ssh_timeout = 5
        self.window_size = 1024
        self.cli_error_re = re.compile('(?P<error>\n%\s.*)\n')
        self.cmc_prompt = '.*configuration\smode\sanyway:\s'
        self.cms_cmd_count = 10


    def getargs(self):
        """
        Collect command line arguments when invoked as a standalone script
        """
        parser = argparse.ArgumentParser(
            description='Reads user named file containing a list of CLI commands and executes these on the named'
                        'EndaceProbe.'
        )
        parser.add_argument('-f', action='store', required=True, dest='file', help='Path to file containing CLI commands')
        parser.add_argument('-d', action='store', required=True, dest='host', help='Name of target probe')
        parser.add_argument('-u', action='store', required=True, dest='user', help = 'Username for probe authentication')
        parser.add_argument('-p', action='store', required=True, dest='password', help = 'Password for probe authentication')
        parser.add_argument('-U', action='store', dest='url', help='URL to location of image file')
        parser.add_argument('-N', action='store', dest='vmname', help='Name to assign to VM')
        parser.add_argument('-v', action='store_true', dest='debug', help='Enable debug output')
        parser.add_argument('-c', action='store', dest='cms_profile',
                            help='CMS Profile name, use when target is a CMS and a profile is being created')

        self.args = parser.parse_args()

        # Setup log file based on name of input file.
        self.logfile = logging.FileHandler('%s.log' % re.split('\.', self.args.file)[0])
        self.logger.addHandler(self.logfile)

        # Need to strip hostname for command prompt regex
        self.hostname = re.split('\.', self.args.host)[0]

        # Macros in CLI script may append additional prompts (from other vendors CLIs)
        self.prompt = ['.*%s\s[#>]\s' % self.hostname, '.*%s\s\(config\)' % self.hostname,
                       'Escape\scharacter\sis:\s\'Ctrl\s\^\'']


        # Set debug level
        if self.args.debug:
            self.logger.setLevel(logging.DEBUG)

        # Populate replacement vars

        if (self.args.url != None):
            self.vars['URL'] = self.args.url
            self.vars['IMAGE'] = re.split('/', self.args.url)[-1]
        else:
            self.logger.info('No URL available, will exit if line parsed that contains URL variable')

        # Don't want to override the default above
        if ( self.vars['VMNAME'] != None ):
            self.vars['VMNAME'] = self.args.vmname


    def open_conn(self):
        """
        Open SSH connection to target probe.  Automatically sets configure mode.
        """
        self.logger.info('Opening connection to %s' % (self.args.host))

        try:
            self.conn = paramiko.SSHClient()
            self.conn.load_system_host_keys()
            self.conn.set_missing_host_key_policy(paramiko.WarningPolicy)
            self.conn.connect(username=self.args.user, password=self.args.password, hostname=self.args.host,
                                 timeout=self.ssh_timeout, allow_agent=False )

        except paramiko.BadHostKeyException as e:
                self.logger.error('Error - Bad host key, cannot connect to %s: %s' % (self.args.host, e))
        except paramiko.AuthenticationException as e:
            self.logger.error('Error - Unable to authenticate, cannot connect to %s: %s' % (self.args.host, e))
        except paramiko.SSHException as e:
            self.logger.error('Error - SSH connection error, cannot connect to %s: %s' % (self.args.host, e))
        except Exception as e:
            self.logger.error('Error - Undefined, cannot connect to %s: %s' % (self.args.host, e))

        self.chan = self.conn.get_transport().open_session()
        self.chan.get_pty(term='vt100', width=80, height=40)
        self.chan.invoke_shell()

        # Clear out response buffer from initial login
        self.parse_command_response()
        self.chan.settimeout(self.ssh_timeout)


    def close_conn(self):
        """
        Close SSH connection
        """
        self.logger.info('Closing connection to %s' % (self.args.host))
        self.conn.close()


    def parse_file(self, macro=False):
        """
        Loop through named file and execute each CLI command.
        Will replace any placeholders in template at runtime with named vars if provided
        """
        self.logger.debug('Opening %s for reading commands' % (self.args.file))

        try:
            for line in open(self.args.file):

                # Skip commented out lines
                if re.search('^#', line): continue

                # Skip blank lines
                if re.search('^\r?\n$', line): continue

                # Parse config file for pre-execution macros
                if macro:
                    if re.search('^@\w+', line):
                        self.parse_macro(line)
                        continue

                else:
                    # Execute run time macros
                    if re.search('^@', line):
                        self.exec_macro(line)
                        continue

                    # Check each line for variable replacements
                    for x in self.vars:
                        pattern = re.compile('<%s>' % x)
                        if re.search(pattern, line):
                            try:
                                newline = re.sub(pattern, self.vars[x], line)
                                line = newline
                            except TypeError:
                                # Catch type error if vars[x] == None meaning caller has not defined replacement
                                self.logger.info('Found line with variable %s but no replacement value defined' % (x))
                                break

                    self.send_command(line)

        except OSError as e:
            self.logger.error('Cannot open file: %s, received error: %s' % (self.args.file, e))


    def append_prompt(self, action):
        """
        Appends supplied regex string to the default list of command prompts to use for end of stream tokens
        """
        self.prompt.append(action)
        self.logger.debug(self.prompt)


    def parse_macro(self, line):
        """
        Executes pre-run macros if @<macro> is found in the source file.  Macros supported:
            - @prompt : used when logging into VM console and command prompts differ from probes
        """
        out = re.search('^@(?P<macro>\w+)\s(?P<action>.*)$', line)

        if re.match(out.group('macro'), 'prompt'):
            self.logger.debug('Adding %s to self.prompt' % out.group('action'))
            self.append_prompt(out.group('action'))

    def exec_macro(self, line):
        """
        Executes run-time macros if @<macro> is found in the source file.  Macros supported:
            - @sleep : used to pause cliconfig for x seconds, used when VMs take time to (re)boot, start a console etc
            - @exit : stop processing config file, useful for testing
        """

        out = re.search('^@(?P<macro>\w+)\s(?P<action>.*)$', line)

        # Exit macro should be run in all modes
        if re.match(out.group('macro'), 'exit'):
            self.logger.info('@exit macro found in config file, terminating.')
            exit()

        # Skip all other run time macros when writing CMS profiles
        if self.args.cms_profile:
            return

        if re.match(out.group('macro'), 'sleep'):
            self.logger.info('@sleep macro found in config file, sleeping for %s seconds' % out.group('action'))
            time.sleep(int(out.group('action')))


    def send_command(self, line):
        """
        Execute command on provided line and check output for errors
        """

        if (self.args.cms_profile):
            if not re.search('^en|co\st', line):
                line = 'cmc profile %s command %d "%s"\n' %(self.args.cms_profile, self.cms_cmd_count, line.rstrip())
                self.cms_cmd_count += 10
        self.logger.info('Sent command: %s' % line.rstrip())
        self.chan.sendall(line)
        self.parse_command_response()


    def parse_command_response(self):
        """
        Polls channel response looking for command prompt strings.  Returns response and any extracted errors.
        """
        stdout = ""
        stderr = ""
        response = b""

        try:
            while not self.chan.exit_status_ready():

                # There is no way to tell when the command response is complete other than looking for a
                # command prompt.  Since the response can be sent in chunks, the regex on the chunks may fail as the
                # matching elements may be sent in separate chunks.  Aggregating the response before attempting
                # to match resolves this.
                response += self.chan.recv(4096)

                self.logger.debug('!! Response: %s' % response)

                # If an EndaceProbe is under CMS control catch the prompt and return YES
                if re.search(self.cmc_prompt, response.decode('utf-8')):
                    self.send_command('YES\n')
                    break

                for x in self.prompt:
                    # self.logger.debug('!! Comparing to %s' % x)
                    if re.search(x, response.decode('utf-8')):
                        # break if we've seen the command prompt return
                        #self.logger.debug('>> %s' % response)

                        stdout = response.decode('utf-8')

                        if stdout:
                            self.logger.debug('Command returned: %s' % response)

                        stderr = self.cli_error_re.search(stdout)
                        if stderr:
                            self.logger.error('Command returned error: %s' % stderr.group('error'))

                        return (stdout, stderr, response)

        except socket.timeout:
            self.logger.debug('Timeout of %s secs exceeded, closing channel' % self.ssh_timeout)


    def read_cpu_virt(self):
        """
        Reads CPUs assigned for vistualization and return them in a integer list
        still under construction
        """
        temp_list = [4,5,6,11,16,17,18,23]
        final_list = []
        halflen = len(temp_list) / 2
        for i in range(0,(len(temp_list)/2)):
            final_list.append(temp_list[i])
            final_list.append(temp_list[i+halflen])
        return final_list


def main():
    sess = EndaceProbeCLISession()
    sess.getargs()
    sess.logger.debug('Filename: %s, Probe: %s, Username: %s, Password: %s'%
                      (sess.args.file, sess.args.host, sess.args.user, sess.args.password) )

    # Need to pre-parse file for macros in case connecting to non-Probe CLI with different command prompts
    sess.parse_file(macro=True)




    sess.cpu_virt_list = sess.read_cpu_virt()
    print sess.cpu_virt_list
    print 'oi'
    print sess.cpu_virt_list
    

    # Open connection to host
    sess.open_conn()

    # Execute commands in file
    sess.parse_file()

    # Close down connection
    sess.close_conn()

if __name__ == "__main__": main()



