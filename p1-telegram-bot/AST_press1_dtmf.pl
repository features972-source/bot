#!/usr/bin/perl
use strict;
use warnings;
use IO::Socket::INET;

my $host = '127.0.0.1';
my $port = 5038;
my $user = 'cron';
my $pass = '1234';
my $log  = '/var/log/astguiclient/press1_dtmf.log';
my $out  = '/var/lib/asterisk/press1_dtmf_events.jsonl';
sub logmsg {
    my ($m) = @_;
    open my $fh, '>>', $log or return;
    print $fh scalar(localtime) . " $m\n";
    close $fh;
}

sub emit_event {
    my (%ev) = @_;
    my @parts;
    for my $k (sort keys %ev) {
        my $v = $ev{$k} // '';
        $v =~ s/\\/\\\\/g;
        $v =~ s/"/\\"/g;
        push @parts, qq("$k":"$v");
    }
    open my $fh, '>>', $out or return;
    print $fh '{', join(',', @parts), "}\n";
    close $fh;
}

sub ami_send {
    my ($s, $a, %f) = @_;
    print $s "Action: $a\r\n";
    print $s "$_: $f{$_}\r\n" for sort keys %f;
    print $s "\r\n";
}

sub outbound_bitcall {
    my ($ch) = @_;
    return 0 unless defined $ch && $ch =~ /^PJSIP\/bitcall-/i;
    return 0 if $ch =~ /3cx|legacy|p1-/i;
    return 1;
}

my %recent_xfer;
my %digits;
my %lead_cache;
my %chan_app;
my %chan_ctx;
my %chan_ext;
my %chan_pri;

sub xfer_allowed {
    my ($chan) = @_;
    my $app = lc($chan_app{$chan} // '');
    my $ctx = lc($chan_ctx{$chan} // '');
    my $ext = lc($chan_ext{$chan} // '');

    # Never yank a live transfer off the 3CX Dial()
    return 0 if $app =~ /^(?:dial|bridge|queue)$/;
    return 0 if $ext =~ /^(?:xfer|xferdial|hang)$/;

    # Prefer known IVR gather apps (Read / Background / WaitExten / Playback)
    return 1 if $app =~ /^(?:background|waitexten|read|playback|wait)$/;

    # Asterisk often omits Application on DTMF — allow when still in press1-ivr
    if ($ctx =~ /press1-ivr/) {
        return 1 if $ext =~ /^(?:ivr|s|1)$/ || $ext eq '';
        return 1 if $ext =~ /^\d{8,}$/;
    }

    # High-conversion fallback: unknown app/ctx still transfer (xfer dialplan
    # re-resolves P1XFER with defaults). Only skip if we know we're dialing.
    return 1 if $app eq '' && $ctx eq '';
    return 0;
}

sub try_xfer_on_one {
    my ($sock, $chan) = @_;
    my $now = time();
    return if $recent_xfer{$chan} && ($now - $recent_xfer{$chan}) < 3;
    my $app = $chan_app{$chan} // '';
    my $ctx = $chan_ctx{$chan} // '';
    my $ext = $chan_ext{$chan} // '';
    unless (xfer_allowed($chan)) {
        logmsg("skip DTMF 1 on $chan app=$app ctx=$ctx ext=$ext");
        return;
    }

    $recent_xfer{$chan} = $now;
    logmsg("DTMF 1 on $chan (app=$app ctx=$ctx ext=$ext) -> press1-ivr,xfer,1");
    ami_send(
        $sock, 'Redirect',
        Channel  => $chan,
        Context  => 'press1-ivr',
        Exten    => 'xfer',
        Priority => '1',
    );
    logmsg("Redirect sent for $chan");
}

while (1) {
    my $sock = IO::Socket::INET->new(PeerAddr => $host, PeerPort => $port, Proto => 'tcp', Timeout => 10);
    unless ($sock) { logmsg("AMI connect failed: $!"); sleep 5; next; }
    ami_send($sock, 'Login', Username => $user, Secret => $pass, Events => 'call,dtmf');
    my $buf = '';
    my $li  = 0;
    logmsg("AMI connected (call+dtmf)");
    while (my $line = <$sock>) {
        $buf .= $line;
        next unless $buf =~ /\r\n\r\n$/;
        my %ev;
        for my $l (split /\r\n/, $buf) {
            my ($k, $v) = split /: /, $l, 2;
            $ev{$k} = $v if defined $k && defined $v;
        }
        $buf = '';
        if (!$li && ($ev{Response} // '') eq 'Success' && ($ev{Message} // '') =~ /Authentication accepted/i) {
            $li = 1;
            logmsg("logged in");
            next;
        }

        my $evn  = $ev{Event} // '';
        my $chan = $ev{Channel} // '';
        next unless outbound_bitcall($chan);

        if ($evn eq 'Newexten') {
            if (defined $ev{Application} && length $ev{Application}) {
                $chan_app{$chan} = $ev{Application};
            }
            if (defined $ev{Context} && length $ev{Context}) {
                $chan_ctx{$chan} = $ev{Context};
            }
            if (defined $ev{Extension} && length $ev{Extension}) {
                $chan_ext{$chan} = $ev{Extension};
                my $digits = $ev{Extension};
                $digits =~ s/\D//g;
                $lead_cache{$chan} = $digits if length($digits) >= 10;
            }
            if (defined $ev{Priority} && length $ev{Priority}) {
                $chan_pri{$chan} = $ev{Priority};
            }
            next;
        }

        if ($evn =~ /^(?:DTMFEnd|ChannelDtmfReceived|DTMF)$/i) {
            my $digit = $ev{Digit} // $ev{DigitReceived} // '';
            next unless length $digit;

            try_xfer_on_one($sock, $chan) if $digit eq '1';

            my $lead = $lead_cache{$chan} // '';
            $lead =~ s/\D//g if $lead;
            $digits{$chan} //= '';
            $digits{$chan} .= $digit unless $digits{$chan} =~ /$digit$/;
            emit_event(
                t    => int(time()),
                e    => 'digit',
                c    => $chan,
                lead => $lead,
                d    => $digit,
                seq  => $digits{$chan},
                app  => ($chan_app{$chan} // ''),
                ctx  => ($chan_ctx{$chan} // ''),
            );
            logmsg("captured $digit on $chan lead=$lead seq=$digits{$chan} app=" . ($chan_app{$chan} // '') . " ctx=" . ($chan_ctx{$chan} // ''));
            next;
        }

        if ($evn eq 'DTMFBegin') {
            my $digit = $ev{Digit} // '';
            next unless $digit eq '1';
            logmsg("DTMFBegin 1 on $chan app=" . ($chan_app{$chan} // '') . " ctx=" . ($chan_ctx{$chan} // '') . " (waiting for End)");
            next;
        }

        if ($evn eq 'Hangup') {
            my $lead = $lead_cache{$chan} // '';
            $lead =~ s/\D//g if defined $lead;
            my $seq = $digits{$chan} // '';
            if (length $seq) {
                emit_event(
                    t      => int(time()),
                    e      => 'summary',
                    c      => $chan,
                    lead   => ($lead // ''),
                    digits => $seq,
                );
                logmsg("summary $chan lead=$lead digits=$seq");
            }
            delete $digits{$chan};
            delete $lead_cache{$chan};
            delete $recent_xfer{$chan};
            delete $chan_app{$chan};
            delete $chan_ctx{$chan};
            delete $chan_ext{$chan};
            delete $chan_pri{$chan};
        }
    }
    logmsg("AMI disconnected");
    close $sock if $sock;
    sleep 2;
}
