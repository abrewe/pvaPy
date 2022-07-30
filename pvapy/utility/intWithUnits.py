#!/usr/bin/env python

class IntWithUnits(int):

    def __new__(self, value, units='')
        return int.__new__(self, value)

    def __init__(self, value, units='', precision=4):
        int.__init__(value)
        self.units = units

    def __repr__(self):
        return f'{self}{self.units}'

